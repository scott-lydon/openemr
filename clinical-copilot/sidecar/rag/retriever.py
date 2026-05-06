"""Top-level Retrieval Augmented Generation (RAG) orchestrator.

``retrieve_evidence(query, k, filters)`` runs the hybrid pipeline:

1. **Query rewrite** — abbreviation/synonym expansion via the
   dictionary rewriter (Phase 4 baseline; Phase 11 swaps in the
   contextual rewriter).
2. **Sparse retrieval** — BM25 against the rewritten query.
3. **Dense retrieval** — vector search against the rewritten query's
   embedding.
4. **Fuse** — Reciprocal Rank Fusion (RRF).
5. **Rerank** — Cohere Rerank v3 if available; otherwise the RRF list
   trims to ``k`` and the trace records ``retrieval.degraded=true``.
6. **Trim** — return top ``k``.

The function takes injected backends so unit tests substitute
``InMemoryGuidelineIndex`` for the sparse and dense halves and a
``StubReranker`` for the cross-encoder. The production wire-up lives
in ``sidecar.main`` (where the FastAPI app picks the real Postgres
backend and Cohere client).

Tracing:

- Every call records ``retrieval.candidates_sparse``,
  ``retrieval.candidates_dense``, ``retrieval.fused_count``,
  ``retrieval.reranker_used``, ``retrieval.degraded``, and
  ``retrieval.query_rewrite_applied``. The retriever returns these
  attributes alongside the snippets so the caller can fan them into
  the active span.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Final, Protocol

from sidecar.rag.embeddings import Embedder
from sidecar.rag.query_rewriter import QueryRewriter
from sidecar.rag.reranker import (
    CohereRerankUnavailable,
    Reranker,
    RerankerIsolationViolation,
)
from sidecar.rag.rrf import reciprocal_rank_fuse
from sidecar.rag.search import SearchHit
from sidecar.rag.types import EvidenceSnippet, Filters, RetrievalMethod


logger = logging.getLogger(__name__)


DEFAULT_TOP_K: Final[int] = 5
SPARSE_OVERSAMPLE: Final[int] = 20
DENSE_OVERSAMPLE: Final[int] = 20


# Backend protocols — the retriever calls them by interface so the
# concrete implementation (Postgres vs in-memory) is irrelevant to
# the orchestrator.

class SparseSearch(Protocol):
    def __call__(
        self,
        query: str,
        *,
        top: int,
        filters: Filters | None,
    ) -> list[SearchHit]:
        ...


class DenseSearch(Protocol):
    def __call__(
        self,
        embedding: list[float],
        *,
        top: int,
        filters: Filters | None,
    ) -> list[SearchHit]:
        ...


@dataclass(frozen=True)
class RetrievalResult:
    """Top-level retrieval response.

    ``snippets`` is the trimmed top-k. ``trace_attributes`` is the
    bag of span attributes the caller emits.
    """

    snippets: list[EvidenceSnippet]
    trace_attributes: dict[str, object]


@dataclass
class HybridRetriever:
    """Pluggable retriever wired up from the protocol seams.

    Construction example (unit test)::

        index = InMemoryGuidelineIndex(chunks=..., embeddings=...)
        retriever = HybridRetriever(
            sparse_search=lambda q, *, top, filters: index.lexical(q, top=top, filters=filters),
            dense_search=lambda emb, *, top, filters: index.semantic(emb, top=top, filters=filters),
            embedder=StubEmbedder(),
            rewriter=DictionaryRewriter(),
            reranker=StubReranker(),
        )
    """

    sparse_search: SparseSearch
    dense_search: DenseSearch
    embedder: Embedder
    rewriter: QueryRewriter
    reranker: Reranker | None

    async def retrieve(
        self,
        query: str,
        *,
        k: int = DEFAULT_TOP_K,
        filters: Filters | None = None,
    ) -> RetrievalResult:
        if k <= 0:
            raise ValueError(f"k must be positive, got {k}")
        if not query.strip():
            return RetrievalResult(
                snippets=[],
                trace_attributes={"retrieval.empty_query": True},
            )

        attributes: dict[str, object] = {
            "retrieval.query_chars": len(query),
            "retrieval.filter_domain_tags": (
                [t.value for t in filters.domain_tags]
                if filters and filters.domain_tags
                else []
            ),
        }

        rewrite = await self.rewriter.rewrite(query)
        attributes["retrieval.query_rewrite_applied"] = bool(rewrite.expansions_applied)
        attributes["retrieval.query_rewrite_version"] = rewrite.version

        sparse_hits = list(
            self.sparse_search(rewrite.rewritten, top=SPARSE_OVERSAMPLE, filters=filters)
        )
        attributes["retrieval.candidates_sparse"] = len(sparse_hits)

        embedding = await self.embedder.embed_query(rewrite.rewritten)
        dense_hits = list(
            self.dense_search(embedding, top=DENSE_OVERSAMPLE, filters=filters)
        )
        attributes["retrieval.candidates_dense"] = len(dense_hits)

        fused = reciprocal_rank_fuse(
            sparse_hits, dense_hits, key=lambda hit: hit.snippet.chunk_id
        )
        attributes["retrieval.fused_count"] = len(fused)

        if not fused:
            return RetrievalResult(snippets=[], trace_attributes=attributes)

        # Tag the fused snippets so the caller can see when a snippet
        # came through fusion vs through direct rerank.
        fused_snippets = [
            hit.snippet.model_copy(update={"retrieval_method": RetrievalMethod.RRF})
            for hit in fused
        ]

        if self.reranker is None:
            attributes["retrieval.reranker_used"] = False
            attributes["retrieval.degraded"] = True
            return RetrievalResult(
                snippets=fused_snippets[:k],
                trace_attributes=attributes,
            )

        try:
            reranked = await self.reranker.rerank(
                query=rewrite.original,
                snippets=fused_snippets,
                top=k,
            )
        except CohereRerankUnavailable as exc:
            logger.warning("Cohere reranker unavailable, using RRF fallback: %s", exc)
            attributes["retrieval.reranker_used"] = False
            attributes["retrieval.degraded"] = True
            attributes["retrieval.degraded_reason"] = str(exc)[:120]
            return RetrievalResult(
                snippets=fused_snippets[:k],
                trace_attributes=attributes,
            )
        except RerankerIsolationViolation:
            # Isolation violation is a bug, not a transient. Re-raise so
            # the retriever never silently downgrades to the fused list
            # when there's actual leakable content. The caller maps this
            # to a 500 with a clear message.
            raise

        attributes["retrieval.reranker_used"] = True
        attributes["retrieval.degraded"] = False
        return RetrievalResult(
            snippets=list(reranked),
            trace_attributes=attributes,
        )


__all__ = [
    "DEFAULT_TOP_K",
    "DENSE_OVERSAMPLE",
    "HybridRetriever",
    "RetrievalResult",
    "SPARSE_OVERSAMPLE",
]
