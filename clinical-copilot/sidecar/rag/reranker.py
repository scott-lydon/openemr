"""Cohere Rerank v3 client with patient-data isolation guard.

Cohere Rerank v3 is a Software-as-a-Service (SaaS) cross-encoder.
Patient data must NEVER be passed to it. The reranker only ever sees:

- The query string the user typed (or its rewritten form).
- The PUBLIC guideline chunk text.

Anything else — patient identifiers, snapshot fields, document
content, span attributes — is forbidden. The isolation guard in this
module enforces the rule at the call boundary so a future bug
elsewhere cannot accidentally leak.

Two implementations:

- ``CohereReranker`` — production. Calls the Cohere ``rerank`` endpoint
  via httpx; raises ``CohereRerankUnavailable`` on any 5xx, timeout,
  or rate-limit so the caller can fall through to the RRF list.
- ``StubReranker`` — deterministic test substitute that scores items by
  a configured strategy (lexical overlap with the query by default).

The isolation guard runs on every call. It scans the request body for
patterns matching patient identifiers (``Patient/<uuid>``,
``MRN<digits>``) and digit groups that look like Social Security
Numbers. A match raises ``RerankerIsolationViolation`` BEFORE the
network call is made, so a regression that introduces a leak fails in
the unit tests, not in production.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Final, Protocol

import httpx

from sidecar.rag.types import EvidenceSnippet, RetrievalMethod


logger = logging.getLogger(__name__)


COHERE_RERANK_ENDPOINT: Final[str] = "https://api.cohere.com/v2/rerank"
COHERE_RERANK_MODEL: Final[str] = "rerank-english-v3.0"


# Patterns the isolation guard scans for. Each is a regex; any match in
# the query or the candidate documents is treated as a leak. The list
# is intentionally conservative: false positives are preferred over
# false negatives because the guard fails closed (refuses the call).
_LEAK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"Patient/[a-fA-F0-9-]{4,}"),
    re.compile(r"\bMRN[0-9]{4,}\b", re.IGNORECASE),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # Social Security Number
    re.compile(r"\bDOB:\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", re.IGNORECASE),
    # patient.identifier and similar JSON paths suggest someone is
    # trying to send a snapshot field. Block by name.
    re.compile(r"\bpatient[._]identifier\b", re.IGNORECASE),
)


class CohereRerankUnavailable(Exception):
    """Cohere is unreachable, rate-limited, or 5xx'ing.

    Caller (the retriever) catches and falls back to the RRF top-k.
    """


class RerankerIsolationViolation(Exception):
    """The reranker call would leak patient data; refused.

    A bug, not a transient. Tests assert this fires on every leakable
    pattern.
    """


def assert_no_phi(query: str, documents: list[str]) -> None:
    """Refuse if any input contains a forbidden pattern.

    Run as the first step of any reranker call. Exposed here so unit
    tests can exercise the guard directly without constructing a
    client.
    """
    for label, body in [("query", query), *((f"document[{i}]", d) for i, d in enumerate(documents))]:
        for pattern in _LEAK_PATTERNS:
            match = pattern.search(body)
            if match:
                raise RerankerIsolationViolation(
                    f"reranker isolation violation in {label}: pattern "
                    f"{pattern.pattern!r} matched. The reranker is allowed "
                    "to see only the query and public guideline chunks. "
                    f"Match preview: ...{body[max(0, match.start() - 20):match.end() + 20]!r}..."
                )


class Reranker(Protocol):
    """The protocol every reranker implementation honors."""

    async def rerank(
        self, *, query: str, snippets: list[EvidenceSnippet], top: int
    ) -> list[EvidenceSnippet]:
        ...


@dataclass
class CohereReranker:
    """Production Cohere Rerank v3 client.

    Configuration via environment:

    - ``COHERE_API_KEY`` — required. Missing key raises
      ``CohereRerankUnavailable`` so the retriever falls back rather
      than crashing.
    - ``COHERE_RERANK_ENDPOINT_OVERRIDE`` — optional, points at a
      proxy or staging endpoint.
    """

    api_key: str | None = None
    endpoint: str = COHERE_RERANK_ENDPOINT
    model: str = COHERE_RERANK_MODEL
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "CohereReranker":
        return cls(
            api_key=os.environ.get("COHERE_API_KEY"),
            endpoint=os.environ.get("COHERE_RERANK_ENDPOINT_OVERRIDE", COHERE_RERANK_ENDPOINT),
        )

    async def rerank(
        self, *, query: str, snippets: list[EvidenceSnippet], top: int
    ) -> list[EvidenceSnippet]:
        if not self.api_key:
            raise CohereRerankUnavailable("COHERE_API_KEY is not set")
        if not snippets:
            return []

        document_texts = [s.text for s in snippets]
        assert_no_phi(query=query, documents=document_texts)

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds)) as client:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "query": query,
                        "documents": document_texts,
                        "top_n": top,
                    },
                )
        except httpx.RequestError as exc:
            raise CohereRerankUnavailable(
                f"network error: {type(exc).__name__}: {exc!s}"
            ) from exc

        if response.status_code in {429, 500, 502, 503, 504}:
            raise CohereRerankUnavailable(
                f"Cohere returned status={response.status_code}"
            )
        if response.status_code != 200:
            raise CohereRerankUnavailable(
                f"unexpected Cohere status={response.status_code} "
                f"body[:512]={response.text[:512]!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CohereRerankUnavailable(
                f"Cohere response was not valid JSON: {exc!s}"
            ) from exc

        results = payload.get("results")
        if not isinstance(results, list):
            raise CohereRerankUnavailable(
                "Cohere response missing 'results' list"
            )

        ordered: list[EvidenceSnippet] = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            index = entry.get("index")
            score = entry.get("relevance_score")
            if not isinstance(index, int) or index < 0 or index >= len(snippets):
                continue
            if not isinstance(score, (int, float)):
                continue
            base = snippets[index]
            ordered.append(
                base.model_copy(
                    update={
                        "relevance_score": max(0.0, min(1.0, float(score))),
                        "retrieval_method": RetrievalMethod.RERANK,
                    }
                )
            )
        return ordered[:top]


@dataclass
class StubReranker:
    """Deterministic reranker for unit tests.

    Default scoring: count of distinct query tokens that appear in the
    snippet text. Perfectly stable across runs because it's pure
    Python; lets tests assert on the exact ordering.
    """

    invocations: list[tuple[str, list[str]]] = field(default_factory=list)

    async def rerank(
        self, *, query: str, snippets: list[EvidenceSnippet], top: int
    ) -> list[EvidenceSnippet]:
        documents = [s.text for s in snippets]
        # Run the isolation guard so tests can assert on it.
        assert_no_phi(query=query, documents=documents)
        self.invocations.append((query, documents))
        terms = {t.lower() for t in query.split() if t}
        scored: list[tuple[EvidenceSnippet, float]] = []
        for snippet in snippets:
            text_lower = snippet.text.lower()
            score = sum(1.0 for t in terms if t in text_lower) / max(1, len(terms))
            scored.append((snippet, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [
            base.model_copy(
                update={
                    "relevance_score": score,
                    "retrieval_method": RetrievalMethod.RERANK,
                }
            )
            for base, score in scored[:top]
        ]


__all__ = [
    "COHERE_RERANK_ENDPOINT",
    "COHERE_RERANK_MODEL",
    "CohereRerankUnavailable",
    "CohereReranker",
    "Reranker",
    "RerankerIsolationViolation",
    "StubReranker",
    "assert_no_phi",
]
