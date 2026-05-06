"""Data Transfer Objects (DTOs) for the Retrieval Augmented Generation
(RAG) pipeline.

Five public types:

- ``GuidelineChunk`` — the indexable unit. One row per chunk in the
  ``guideline_chunks`` table.
- ``EvidenceSnippet`` — a retrieved chunk with a relevance score and a
  retrieval-method tag. The agent's evidence packet is built from
  these.
- ``Filters`` — optional domain restriction passed to the retriever.
- ``EmbeddingVector`` — a typed wrapper for the embedding so a future
  model swap surfaces at type-check time.
- ``RetrievalMethod`` — an enum naming the path each snippet came from
  (``bm25``, ``vector``, ``rrf``, ``rerank``); the agent shows this
  in the citation card so a clinician knows whether the chunk arrived
  via lexical match or semantic similarity.

Every DTO is frozen Pydantic with ``extra='forbid'``. Domain tags are
the closed set in ``DomainTag`` so a typo in a filter call site fails
at type-check time.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class DomainTag(str, Enum):
    """Closed set of clinical domains the corpus chunker labels.

    Any chunk that does not match a known tag is left untagged; the
    retriever's domain filter then acts as a no-op for that chunk. The
    enum is closed so a tag typo in a filter is a type-check error,
    not a silent miss.
    """

    DIABETES = "diabetes"
    HYPERTENSION = "hypertension"
    LIPIDS = "lipids"
    SCREENING = "screening"
    IMMUNIZATION = "immunization"
    GOUT = "gout"
    OSTEOPOROSIS = "osteoporosis"
    MENTAL_HEALTH = "mental_health"
    CARDIOVASCULAR = "cardiovascular"
    ONCOLOGY = "oncology"


class RetrievalMethod(str, Enum):
    """How a snippet arrived at the result list.

    ``BM25`` — sparse lexical match; ``VECTOR`` — dense semantic match;
    ``RRF`` — Reciprocal Rank Fusion of the two; ``RERANK`` — Cohere
    Rerank v3 reordered the fused list.
    """

    BM25 = "bm25"
    VECTOR = "vector"
    RRF = "rrf"
    RERANK = "rerank"


class EmbeddingVector(BaseModel):
    """A typed embedding vector.

    The model name and dimension are recorded so a future re-embedding
    migration can be audited end-to-end (the index migration writes the
    same name/dimension into the row, the retriever reads it back, and
    a mismatch raises rather than silently using stale embeddings).
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    model: str
    dimension: int = Field(ge=1, le=4096)
    values: list[float]


class GuidelineChunk(BaseModel):
    """One indexable chunk of clinical guideline content."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    chunk_id: str
    source_id: str
    section_path: str
    anchor_url: str
    domain_tags: list[DomainTag]
    text: str
    embedding_model: str
    license_url: str


class EvidenceSnippet(BaseModel):
    """A retrieved chunk with its score and provenance.

    The agent's response packet uses this directly to populate citation
    chips. ``relevance_score`` is in the [0, 1] range whether the snippet
    came through reranking (Cohere Rerank v3) or RRF (no reranker
    available).
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    chunk_id: str
    source_id: str
    section: str
    anchor_url: str
    text: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    retrieval_method: RetrievalMethod
    domain_tags: list[DomainTag]


class Filters(BaseModel):
    """Optional retrieval restrictions.

    ``domain_tags`` restricts to chunks carrying at least one of the
    listed tags. ``None`` means no restriction. The retriever passes the
    filter into both BM25 and vector searches, so a chunk excluded by
    the filter never reaches the fuser.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    domain_tags: list[DomainTag] | None = None
    age_band: str | None = None  # Phase 11 contextual rewriter wires this.
    sex_specific: str | None = None


__all__ = [
    "DomainTag",
    "EmbeddingVector",
    "EvidenceSnippet",
    "Filters",
    "GuidelineChunk",
    "RetrievalMethod",
]
