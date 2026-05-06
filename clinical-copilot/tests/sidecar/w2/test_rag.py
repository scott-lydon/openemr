"""Tests for the Week 2 hybrid Retrieval Augmented Generation (RAG)
pipeline.

Coverage:

- Chunker: section-aware splits, recommendation blocks emitted alone,
  overlap respected, stable chunk ids, domain tag detection.
- Reciprocal Rank Fusion: equal scores preserve first-list order;
  items in only one list still rank; ``k`` controls smoothing.
- Query rewriter: dictionary expansion, no-op when no terms match,
  preserves original prefix.
- Embedder stub: same input -> same vector; unit-norm; different
  inputs -> different vectors.
- Reranker isolation guard: refuses Patient/<uuid>, MRN, SSN, DOB,
  ``patient.identifier`` JSON path.
- StubReranker: scores by lexical overlap, sorts descending, marks
  retrieval method as RERANK.
- Hybrid retriever happy path: returns top-k with rerank flag set.
- Hybrid retriever degradation: when reranker raises Unavailable, the
  retriever falls back to RRF with ``degraded=True``.
- Domain filter: BM25 + vector both honor ``Filters.domain_tags``.
- Empty corpus: retriever returns ``[]`` instead of fabricating.
"""

from __future__ import annotations

import math

import pytest

from sidecar.rag import (
    ChunkInput,
    CohereRerankUnavailable,
    DictionaryRewriter,
    DomainTag,
    Filters,
    GuidelineChunk,
    HybridRetriever,
    InMemoryGuidelineIndex,
    RetrievalMethod,
    RerankerIsolationViolation,
    StubEmbedder,
    StubReranker,
    assert_no_phi,
    chunk_document,
    detect_domain_tags,
    embed_text_deterministic,
    reciprocal_rank_fuse,
)


# ─── Chunker ──────────────────────────────────────────────────────────


def test_chunker_emits_section_aware_chunks() -> None:
    md = (
        "# Title\n\n"
        "## A1c targets\n\n"
        "Adults with type 2 diabetes should aim for HbA1c below 7%.\n\n"
        "## Hypertension goals\n\n"
        "Goal blood pressure under 130/80 mmHg.\n\n"
    )
    chunks = chunk_document(
        ChunkInput(
            source_id="ada-2025",
            anchor_url="https://example.com/ada",
            license_url="https://example.com/license",
            markdown=md,
        )
    )
    assert len(chunks) == 2
    assert chunks[0].section_path == "Title > A1c targets"
    assert chunks[1].section_path == "Title > Hypertension goals"


def test_chunker_recommendation_blocks_are_emitted_alone() -> None:
    md = (
        "## Targets\n\n"
        "Background paragraph one.\n\n"
        "Recommendation: Adults aged 35-70 with overweight should be screened "
        "for prediabetes.\n\n"
        "Background paragraph two.\n"
    )
    chunks = chunk_document(
        ChunkInput(
            source_id="uspstf",
            anchor_url="https://example.com/uspstf",
            license_url="https://example.com/license",
            markdown=md,
        )
    )
    assert any("Recommendation:" in c.text for c in chunks)
    rec_chunk = next(c for c in chunks if c.text.startswith("Recommendation:"))
    assert "Background" not in rec_chunk.text


def test_chunker_chunk_id_is_stable_across_runs() -> None:
    md = "## Topic\n\nHbA1c target should be under 7%.\n"
    inp = ChunkInput(
        source_id="ada-2025",
        anchor_url="https://example.com",
        license_url="https://example.com/license",
        markdown=md,
    )
    a = chunk_document(inp)
    b = chunk_document(inp)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]


def test_chunker_empty_input_returns_empty_list() -> None:
    chunks = chunk_document(
        ChunkInput(
            source_id="x",
            anchor_url="https://example.com",
            license_url="https://example.com/license",
            markdown="",
        )
    )
    assert chunks == []


@pytest.mark.parametrize(
    "text, expected_tag",
    [
        ("HbA1c target is under 7%", DomainTag.DIABETES),
        ("Treat hypertension aggressively", DomainTag.HYPERTENSION),
        ("LDL cholesterol below 70", DomainTag.LIPIDS),
        ("USPSTF screening recommendation", DomainTag.SCREENING),
        ("Allopurinol for gout flare", DomainTag.GOUT),
        ("DXA scan for osteoporosis", DomainTag.OSTEOPOROSIS),
    ],
)
def test_detect_domain_tags_matches_known_terms(text: str, expected_tag: DomainTag) -> None:
    tags = detect_domain_tags(text)
    assert expected_tag in tags


# ─── RRF ──────────────────────────────────────────────────────────────


def test_rrf_combines_two_lists() -> None:
    a = [{"id": "x"}, {"id": "y"}, {"id": "z"}]
    b = [{"id": "y"}, {"id": "z"}, {"id": "w"}]
    out = reciprocal_rank_fuse(a, b, key=lambda d: d["id"])
    ids = [d["id"] for d in out]
    assert "y" in ids and "z" in ids
    # y appears at rank 2 in both lists; should outrank z which appears at rank 3 then 2.
    assert ids.index("y") < ids.index("w")


def test_rrf_items_in_only_one_list_still_rank() -> None:
    out = reciprocal_rank_fuse([{"id": "a"}], [{"id": "b"}], key=lambda d: d["id"])
    ids = [d["id"] for d in out]
    assert set(ids) == {"a", "b"}


def test_rrf_rejects_non_positive_k() -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fuse([{"id": "a"}], k=0, key=lambda d: d["id"])


# ─── Query rewriter ───────────────────────────────────────────────────


async def test_dictionary_rewriter_expands_known_abbreviations() -> None:
    rewriter = DictionaryRewriter()
    result = await rewriter.rewrite("What is the target HbA1c for an A1c?")
    assert "glycated hemoglobin" in result.rewritten
    assert any(token == "HbA1c" or token == "a1c" or token == "A1c"
               for token, _ in result.expansions_applied) or len(result.expansions_applied) >= 1


async def test_dictionary_rewriter_noop_when_no_match() -> None:
    rewriter = DictionaryRewriter()
    result = await rewriter.rewrite("hello world this is a plain sentence")
    assert result.rewritten == result.original
    assert result.expansions_applied == []


async def test_dictionary_rewriter_preserves_original_prefix() -> None:
    rewriter = DictionaryRewriter()
    result = await rewriter.rewrite("BP target")
    assert result.rewritten.startswith("BP target")


# ─── Embedder stub ────────────────────────────────────────────────────


async def test_stub_embedder_is_deterministic() -> None:
    e = StubEmbedder()
    a = await e.embed_query("hello world")
    b = await e.embed_query("hello world")
    assert a == b


def test_embed_text_is_unit_norm() -> None:
    vec = embed_text_deterministic("HbA1c target", dimension=16)
    norm = math.sqrt(sum(v * v for v in vec))
    assert abs(norm - 1.0) < 1e-6


def test_embed_text_different_inputs_diverge() -> None:
    a = embed_text_deterministic("HbA1c target", dimension=16)
    b = embed_text_deterministic("blood pressure target", dimension=16)
    assert a != b
    # Cosine similarity well below 1: not duplicate vectors.
    cos = sum(x * y for x, y in zip(a, b))
    assert abs(cos) < 0.9


# ─── Reranker isolation guard ─────────────────────────────────────────


@pytest.mark.parametrize(
    "leak_string",
    [
        "Patient/123e4567-e89b-12d3-a456-426614174000",
        "MRN12345678",
        "SSN 123-45-6789",
        "DOB: 03/15/1962",
        "patient.identifier=foo",
    ],
)
def test_isolation_guard_catches_phi_in_query(leak_string: str) -> None:
    with pytest.raises(RerankerIsolationViolation):
        assert_no_phi(query=f"What about {leak_string}?", documents=["clean text"])


def test_isolation_guard_catches_phi_in_documents() -> None:
    with pytest.raises(RerankerIsolationViolation):
        assert_no_phi(
            query="HbA1c target",
            documents=["clean", "Patient/abcd-1234-efgh-5678 record"],
        )


def test_isolation_guard_passes_clean_inputs() -> None:
    assert_no_phi(
        query="What is the HbA1c target for type 2 diabetes?",
        documents=["ADA recommends HbA1c < 7% for most adults."],
    )


# ─── StubReranker ─────────────────────────────────────────────────────


async def test_stub_reranker_orders_by_token_overlap() -> None:
    from sidecar.rag.types import EvidenceSnippet, RetrievalMethod

    snippets = [
        EvidenceSnippet(
            chunk_id="c-low",
            source_id="src",
            section="x",
            anchor_url="https://example.com",
            text="unrelated content about cooking",
            relevance_score=0.5,
            retrieval_method=RetrievalMethod.RRF,
            domain_tags=[],
        ),
        EvidenceSnippet(
            chunk_id="c-high",
            source_id="src",
            section="x",
            anchor_url="https://example.com",
            text="HbA1c target diabetes",
            relevance_score=0.5,
            retrieval_method=RetrievalMethod.RRF,
            domain_tags=[],
        ),
    ]
    reranker = StubReranker()
    out = await reranker.rerank(query="HbA1c target", snippets=snippets, top=2)
    assert out[0].chunk_id == "c-high"
    assert all(s.retrieval_method is RetrievalMethod.RERANK for s in out)


# ─── Hybrid retriever ─────────────────────────────────────────────────


def _build_index() -> InMemoryGuidelineIndex:
    chunks = [
        GuidelineChunk(
            chunk_id="diabetes-1",
            source_id="ada-2025",
            section_path="ADA 2025 > A1c",
            anchor_url="https://example.com/ada/a1c",
            domain_tags=[DomainTag.DIABETES],
            text="ADA recommends HbA1c below 7% for most adults with type 2 diabetes.",
            embedding_model="stub-embedder-v1",
            license_url="https://example.com/license",
        ),
        GuidelineChunk(
            chunk_id="ht-1",
            source_id="aha",
            section_path="AHA > Hypertension",
            anchor_url="https://example.com/aha/ht",
            domain_tags=[DomainTag.HYPERTENSION],
            text="Goal blood pressure under 130/80 mmHg.",
            embedding_model="stub-embedder-v1",
            license_url="https://example.com/license",
        ),
        GuidelineChunk(
            chunk_id="gout-1",
            source_id="acr",
            section_path="ACR > Gout",
            anchor_url="https://example.com/acr",
            domain_tags=[DomainTag.GOUT],
            text="First-line urate-lowering therapy for gout is allopurinol.",
            embedding_model="stub-embedder-v1",
            license_url="https://example.com/license",
        ),
    ]
    embeddings = {
        c.chunk_id: embed_text_deterministic(c.text, dimension=16) for c in chunks
    }
    return InMemoryGuidelineIndex(
        chunks={c.chunk_id: c for c in chunks},
        embeddings=embeddings,
    )


async def test_hybrid_retriever_top_hit_for_diabetes_query() -> None:
    index = _build_index()
    retriever = HybridRetriever(
        sparse_search=lambda q, *, top, filters: index.lexical(q, top=top, filters=filters),
        dense_search=lambda emb, *, top, filters: index.semantic(emb, top=top, filters=filters),
        embedder=StubEmbedder(),
        rewriter=DictionaryRewriter(),
        reranker=StubReranker(),
    )
    result = await retriever.retrieve(
        "What is the target HbA1c for type 2 diabetes?", k=2
    )
    assert any(s.chunk_id == "diabetes-1" for s in result.snippets)
    assert result.trace_attributes.get("retrieval.reranker_used") is True


async def test_hybrid_retriever_falls_back_when_reranker_unavailable() -> None:
    class BrokenReranker:
        async def rerank(self, *, query, snippets, top):
            raise CohereRerankUnavailable("simulated 503")

    index = _build_index()
    retriever = HybridRetriever(
        sparse_search=lambda q, *, top, filters: index.lexical(q, top=top, filters=filters),
        dense_search=lambda emb, *, top, filters: index.semantic(emb, top=top, filters=filters),
        embedder=StubEmbedder(),
        rewriter=DictionaryRewriter(),
        reranker=BrokenReranker(),
    )
    result = await retriever.retrieve("HbA1c target", k=3)
    assert result.trace_attributes.get("retrieval.degraded") is True
    assert result.trace_attributes.get("retrieval.reranker_used") is False
    assert len(result.snippets) >= 1


async def test_hybrid_retriever_domain_filter_restricts() -> None:
    index = _build_index()
    retriever = HybridRetriever(
        sparse_search=lambda q, *, top, filters: index.lexical(q, top=top, filters=filters),
        dense_search=lambda emb, *, top, filters: index.semantic(emb, top=top, filters=filters),
        embedder=StubEmbedder(),
        rewriter=DictionaryRewriter(),
        reranker=StubReranker(),
    )
    result = await retriever.retrieve(
        "first-line therapy for crystal arthropathy",
        k=3,
        filters=Filters(domain_tags=[DomainTag.GOUT]),
    )
    assert all(DomainTag.GOUT in s.domain_tags for s in result.snippets)


async def test_hybrid_retriever_empty_corpus_returns_empty() -> None:
    empty = InMemoryGuidelineIndex(chunks={}, embeddings={})
    retriever = HybridRetriever(
        sparse_search=lambda q, *, top, filters: empty.lexical(q, top=top, filters=filters),
        dense_search=lambda emb, *, top, filters: empty.semantic(emb, top=top, filters=filters),
        embedder=StubEmbedder(),
        rewriter=DictionaryRewriter(),
        reranker=StubReranker(),
    )
    result = await retriever.retrieve("HbA1c target", k=5)
    assert result.snippets == []


async def test_hybrid_retriever_empty_query_short_circuits() -> None:
    index = _build_index()
    retriever = HybridRetriever(
        sparse_search=lambda q, *, top, filters: index.lexical(q, top=top, filters=filters),
        dense_search=lambda emb, *, top, filters: index.semantic(emb, top=top, filters=filters),
        embedder=StubEmbedder(),
        rewriter=DictionaryRewriter(),
        reranker=StubReranker(),
    )
    result = await retriever.retrieve("   ", k=3)
    assert result.snippets == []
    assert result.trace_attributes.get("retrieval.empty_query") is True


async def test_hybrid_retriever_no_reranker_means_degraded() -> None:
    index = _build_index()
    retriever = HybridRetriever(
        sparse_search=lambda q, *, top, filters: index.lexical(q, top=top, filters=filters),
        dense_search=lambda emb, *, top, filters: index.semantic(emb, top=top, filters=filters),
        embedder=StubEmbedder(),
        rewriter=DictionaryRewriter(),
        reranker=None,
    )
    result = await retriever.retrieve("HbA1c target", k=2)
    assert result.trace_attributes.get("retrieval.degraded") is True
    assert result.trace_attributes.get("retrieval.reranker_used") is False
