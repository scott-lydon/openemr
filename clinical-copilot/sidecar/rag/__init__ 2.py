"""Retrieval Augmented Generation (RAG) pipeline.

Public surface re-exported here so callers can write ``from sidecar.rag
import HybridRetriever`` without reaching into module paths.

Modules:

- ``types`` — DTOs (chunk, snippet, filter, embedding, retrieval method).
- ``chunker`` — section-aware Markdown chunker for the corpus.
- ``embeddings`` — embedder protocol + deterministic stub.
- ``query_rewriter`` — synonym/abbreviation expansion (Phase 4 baseline).
- ``search`` — Postgres BM25 + vector search backends (and an in-memory
  test substitute).
- ``rrf`` — Reciprocal Rank Fusion of multiple ranked lists.
- ``reranker`` — Cohere Rerank v3 client + isolation guard + stub.
- ``retriever`` — hybrid orchestrator that ties it all together.
"""

from sidecar.rag.chunker import (
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    ChunkInput,
    RawChunk,
    chunk_document,
    detect_domain_tags,
)
from sidecar.rag.embeddings import (
    DEFAULT_PRODUCTION_DIM,
    Embedder,
    STUB_DIM,
    StubEmbedder,
    embed_text_deterministic,
)
from sidecar.rag.query_rewriter import (
    DEFAULT_ABBREVIATION_DICTIONARY,
    DICTIONARY_REWRITER_VERSION,
    DictionaryRewriter,
    QueryRewriter,
    RewriteResult,
)
from sidecar.rag.reranker import (
    COHERE_RERANK_ENDPOINT,
    COHERE_RERANK_MODEL,
    CohereRerankUnavailable,
    CohereReranker,
    Reranker,
    RerankerIsolationViolation,
    StubReranker,
    assert_no_phi,
)
from sidecar.rag.retriever import (
    DEFAULT_TOP_K,
    DENSE_OVERSAMPLE,
    HybridRetriever,
    RetrievalResult,
    SPARSE_OVERSAMPLE,
)
from sidecar.rag.rrf import DEFAULT_K, reciprocal_rank_fuse
from sidecar.rag.search import (
    InMemoryGuidelineIndex,
    SearchConnection,
    SearchCursor,
    SearchHit,
    bm25_search,
    vector_search,
)
from sidecar.rag.types import (
    DomainTag,
    EmbeddingVector,
    EvidenceSnippet,
    Filters,
    GuidelineChunk,
    RetrievalMethod,
)

__all__ = [
    "COHERE_RERANK_ENDPOINT",
    "COHERE_RERANK_MODEL",
    "CohereRerankUnavailable",
    "CohereReranker",
    "ChunkInput",
    "DEFAULT_ABBREVIATION_DICTIONARY",
    "DEFAULT_K",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_PRODUCTION_DIM",
    "DEFAULT_TARGET_TOKENS",
    "DEFAULT_TOP_K",
    "DENSE_OVERSAMPLE",
    "DICTIONARY_REWRITER_VERSION",
    "DictionaryRewriter",
    "DomainTag",
    "Embedder",
    "EmbeddingVector",
    "EvidenceSnippet",
    "Filters",
    "GuidelineChunk",
    "HybridRetriever",
    "InMemoryGuidelineIndex",
    "QueryRewriter",
    "RawChunk",
    "Reranker",
    "RerankerIsolationViolation",
    "RetrievalMethod",
    "RetrievalResult",
    "RewriteResult",
    "SPARSE_OVERSAMPLE",
    "STUB_DIM",
    "SearchConnection",
    "SearchCursor",
    "SearchHit",
    "StubEmbedder",
    "StubReranker",
    "assert_no_phi",
    "bm25_search",
    "chunk_document",
    "detect_domain_tags",
    "embed_text_deterministic",
    "reciprocal_rank_fuse",
    "vector_search",
]
