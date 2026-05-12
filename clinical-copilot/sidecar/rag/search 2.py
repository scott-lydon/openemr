"""Sparse and dense search abstractions over the corpus.

Two search functions:

- ``bm25_search`` — Postgres full-text-search via the ``text_tsv``
  generated tsvector column. The score is ``ts_rank_cd`` normalized to
  [0, 1]. Domain filter restricts on the ``domain_tags`` array column
  using GIN intersection.
- ``vector_search`` — pgvector cosine-similarity nearest-neighbor over
  the HNSW index. Score is ``1 - cosine_distance`` so it lands in
  [0, 1] with higher = better.

Why both:

- BM25 catches lexical near-matches the embedder would miss
  (rare proper nouns, exact codes like LOINC numbers).
- Vector search catches semantic matches the lexical search misses
  (paraphrases, synonyms not in the rewriter dictionary).
- Reciprocal Rank Fusion of the two is the retrieval-system literature's
  standard hybrid; see ``rrf.py``.

Both functions accept a ``Connection`` Protocol, like the queue
module, so unit tests can substitute a deterministic in-memory model
of the table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sidecar.rag.types import (
    DomainTag,
    EvidenceSnippet,
    Filters,
    GuidelineChunk,
    RetrievalMethod,
)


logger = logging.getLogger(__name__)


class SearchConnection(Protocol):
    """Subset of a database connection used by search."""

    def execute(self, sql: str, params: tuple[object, ...] | None = ...) -> "SearchCursor":
        ...


class SearchCursor(Protocol):
    def fetchall(self) -> list[tuple[object, ...]]:
        ...


@dataclass(frozen=True)
class SearchHit:
    """An intermediate hit before fusion / reranking.

    ``snippet`` is the externally-visible DTO; ``raw_score`` is the
    backend-specific raw score for diagnostics. The retrieval method on
    ``snippet`` is the BACKEND method (``BM25`` or ``VECTOR``); RRF
    rewrites it later.
    """

    snippet: EvidenceSnippet
    raw_score: float


def bm25_search(
    *,
    conn: SearchConnection,
    rewritten_query: str,
    top: int,
    filters: Filters | None = None,
) -> list[SearchHit]:
    """Sparse retrieval via Postgres full-text search.

    Returns up to ``top`` hits sorted descending by ``ts_rank_cd``. The
    score is normalized to [0, 1] by dividing by ``(score + 1.0)``,
    which is monotonic and bounded.
    """
    if top <= 0:
        raise ValueError(f"top must be positive, got {top}")
    if not rewritten_query.strip():
        return []

    domain_tag_values = _collect_domain_filter(filters)
    sql = """
        SELECT chunk_id, source_id, section_path, anchor_url, text,
               domain_tags,
               ts_rank_cd(text_tsv, plainto_tsquery('english', %s)) AS score
        FROM guideline_chunks
        WHERE text_tsv @@ plainto_tsquery('english', %s)
    """
    params: list[object] = [rewritten_query, rewritten_query]
    if domain_tag_values is not None:
        sql += " AND domain_tags && %s::text[]"
        params.append(domain_tag_values)
    sql += " ORDER BY score DESC LIMIT %s;"
    params.append(top)

    cur = conn.execute(sql, tuple(params))
    return [_row_to_hit(row, RetrievalMethod.BM25) for row in cur.fetchall()]


def vector_search(
    *,
    conn: SearchConnection,
    embedding: list[float],
    top: int,
    filters: Filters | None = None,
) -> list[SearchHit]:
    """Dense retrieval via pgvector HNSW.

    The cosine distance operator (``<=>``) ranks ascending (smaller is
    closer), so we negate to get a descending score and then map to
    [0, 1] via ``1 - distance``.
    """
    if top <= 0:
        raise ValueError(f"top must be positive, got {top}")
    if not embedding:
        raise ValueError("embedding must be non-empty")

    domain_tag_values = _collect_domain_filter(filters)
    sql = """
        SELECT chunk_id, source_id, section_path, anchor_url, text,
               domain_tags,
               1.0 - (embedding <=> %s::vector) AS score
        FROM guideline_chunks
        WHERE embedding IS NOT NULL
    """
    params: list[object] = [_format_vector(embedding)]
    if domain_tag_values is not None:
        sql += " AND domain_tags && %s::text[]"
        params.append(domain_tag_values)
    sql += " ORDER BY embedding <=> %s::vector LIMIT %s;"
    params.append(_format_vector(embedding))
    params.append(top)

    cur = conn.execute(sql, tuple(params))
    return [_row_to_hit(row, RetrievalMethod.VECTOR) for row in cur.fetchall()]


def _row_to_hit(row: tuple[object, ...], method: RetrievalMethod) -> SearchHit:
    chunk_id, source_id, section_path, anchor_url, text, domain_tags_raw, score_raw = row
    score = max(0.0, min(1.0, float(score_raw)))  # type: ignore[arg-type]
    domain_tags: list[DomainTag] = []
    if isinstance(domain_tags_raw, list):
        for value in domain_tags_raw:
            try:
                domain_tags.append(DomainTag(str(value)))
            except ValueError:
                # An unknown tag in the database is data drift; log and
                # skip rather than crash the retrieval call.
                logger.warning("unknown domain tag %r in chunk %r", value, chunk_id)
    snippet = EvidenceSnippet(
        chunk_id=str(chunk_id),
        source_id=str(source_id),
        section=str(section_path),
        anchor_url=str(anchor_url),
        text=str(text),
        relevance_score=score,
        retrieval_method=method,
        domain_tags=domain_tags,
    )
    return SearchHit(snippet=snippet, raw_score=float(score))


def _collect_domain_filter(filters: Filters | None) -> list[str] | None:
    if filters is None or not filters.domain_tags:
        return None
    return [tag.value for tag in filters.domain_tags]


def _format_vector(embedding: list[float]) -> str:
    """pgvector accepts the vector literal as a bracketed string."""
    return "[" + ",".join(f"{v:.6f}" for v in embedding) + "]"


# ─── In-memory fake for unit tests ────────────────────────────────────


@dataclass
class InMemoryGuidelineIndex:
    """Test-only deterministic index over a list of ``GuidelineChunk``.

    Implements both BM25-shaped lexical match (token presence) and
    vector-shaped match (cosine of stored synthetic vectors). Tests
    construct one of these, populate ``chunks`` and ``embeddings``, and
    pass it to retriever code via the protocol seam. Removes the need
    to spin up Postgres for unit tests.
    """

    chunks: dict[str, GuidelineChunk]
    embeddings: dict[str, list[float]]

    def lexical(
        self, query: str, *, top: int, filters: Filters | None = None
    ) -> list[SearchHit]:
        terms = [t.lower() for t in query.split() if t.strip()]
        if not terms:
            return []
        candidates = list(_apply_domain_filter(self.chunks.values(), filters))
        scored = [
            (
                chunk,
                sum(1 for t in terms if t in chunk.text.lower()),
            )
            for chunk in candidates
        ]
        scored = [pair for pair in scored if pair[1] > 0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        scored = scored[:top]
        return [
            SearchHit(
                snippet=_chunk_to_snippet(chunk, score / max(1, len(terms)), RetrievalMethod.BM25),
                raw_score=float(score),
            )
            for chunk, score in scored
        ]

    def semantic(
        self, embedding: list[float], *, top: int, filters: Filters | None = None
    ) -> list[SearchHit]:
        from math import sqrt

        candidates = list(_apply_domain_filter(self.chunks.values(), filters))
        scored: list[tuple[GuidelineChunk, float]] = []
        for chunk in candidates:
            vec = self.embeddings.get(chunk.chunk_id)
            if not vec or len(vec) != len(embedding):
                continue
            num = sum(a * b for a, b in zip(vec, embedding))
            denom = sqrt(sum(a * a for a in vec)) * sqrt(sum(b * b for b in embedding))
            cos = num / denom if denom else 0.0
            scored.append((chunk, max(0.0, min(1.0, (cos + 1) / 2))))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        scored = scored[:top]
        return [
            SearchHit(
                snippet=_chunk_to_snippet(chunk, score, RetrievalMethod.VECTOR),
                raw_score=score,
            )
            for chunk, score in scored
        ]


def _apply_domain_filter(chunks, filters: Filters | None):
    if filters is None or not filters.domain_tags:
        yield from chunks
        return
    targets = set(filters.domain_tags)
    for chunk in chunks:
        if any(tag in targets for tag in chunk.domain_tags):
            yield chunk


def _chunk_to_snippet(
    chunk: GuidelineChunk, score: float, method: RetrievalMethod
) -> EvidenceSnippet:
    return EvidenceSnippet(
        chunk_id=chunk.chunk_id,
        source_id=chunk.source_id,
        section=chunk.section_path,
        anchor_url=chunk.anchor_url,
        text=chunk.text,
        relevance_score=score,
        retrieval_method=method,
        domain_tags=list(chunk.domain_tags),
    )


__all__ = [
    "InMemoryGuidelineIndex",
    "SearchConnection",
    "SearchCursor",
    "SearchHit",
    "bm25_search",
    "vector_search",
]
