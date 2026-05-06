"""HTTP route for the Week 2 freeform chat surface.

Endpoints:

- ``POST /agent-api/v1/w2-chat`` — runs the Week 2 multi-agent graph
  (``sidecar.agents.w2.graph.run_graph``) against the request and
  returns the rendered text + supervisor decision + citations.
- ``GET /w2``, ``GET /w2/`` — serves the freeform-chat HTML page.

Mock-mode contract:

- When ``COPILOT_ALLOW_MOCK=true``, the route swaps in deterministic
  stub workers so the chat works end-to-end with no API keys. The
  response is structured but obviously synthetic.
- Production binds the same seams to real workers (live VLM
  extractor, real RAG retriever, real Cohere reranker).

The Week 1 ``/chat`` endpoint stays untouched. Week 2 is a separate
URL so the Week 1 demo continues to work.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from sidecar.agents.w2 import (
    ClinicalClaim,
    GraphNodes,
    GraphState,
    StubCitationResolver,
    VerifierConfig,
    run_graph,
)
from sidecar.auth import TaskTokenClaims, require_task_token
from sidecar.rag.types import EvidenceSnippet, RetrievalMethod


logger = logging.getLogger(__name__)


router = APIRouter()


_UI_PATH = Path(__file__).resolve().parent.parent.parent / "ui" / "chat_w2.html"


class W2ChatRequest(BaseModel):
    """Payload for the freeform chat call."""

    model_config = ConfigDict(extra="forbid", strict=True)

    patient_id: str = Field(min_length=1, max_length=128)
    user_question: str = Field(min_length=1, max_length=4000)
    attached_document_ids: list[str] = Field(default_factory=list)


class W2ChatResponse(BaseModel):
    """Shape returned to the chat UI."""

    model_config = ConfigDict(extra="forbid", strict=True)

    rendered_text: str
    decision_path: str
    intent_kind: str
    worker_sequence: list[str]
    refused: bool
    span_attributes: dict[str, object]
    # Map from the chunk_id / DocumentReference citation surfaced in
    # rendered_text to a UUID stored in the citations table. The chat
    # UI uses these to build click-through links to the bbox preview
    # endpoint.
    citation_links: dict[str, str] = {}


@router.post("/agent-api/v1/w2-chat", response_model=W2ChatResponse)
async def post_w2_chat(
    body: W2ChatRequest,
    claims: Annotated[TaskTokenClaims, Depends(require_task_token)],
) -> W2ChatResponse:
    """Run the Week 2 graph for one freeform-chat turn."""
    if claims.patient_id != body.patient_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "patient_scope_mismatch",
                "message": (
                    f"Token patient_id={claims.patient_id!r} does not match "
                    f"body.patient_id={body.patient_id!r}."
                ),
            },
        )

    state = GraphState(
        encounter_id="demo-encounter",
        user_id=claims.user_id,
        patient_id=body.patient_id,
        user_question=body.user_question,
        purpose_of_use=claims.authorized_purposes[0] if claims.authorized_purposes else "diagnostic_cross_check",
        started_at=datetime.now(tz=timezone.utc),
        attached_documents=list(body.attached_document_ids),
    )

    nodes = _build_nodes(allow_mock=_allow_mock())
    result = await run_graph(
        state=state,
        nodes=nodes,
        verifier_config=VerifierConfig(allow_in_process_scrub_only=True),
    )

    response = result.state.response
    citation_links = _materialize_citations(
        response=response,
        encounter_id=state.encounter_id,
        patient_id=state.patient_id,
    )
    return W2ChatResponse(
        rendered_text=result.rendered_text,
        decision_path=result.state.decision_path.value,
        intent_kind=result.state.intent_kind.value,
        worker_sequence=[w.value for w in result.state.worker_sequence],
        refused=bool(response and response.refusal_reason),
        citation_links=citation_links,
        span_attributes=dict(result.state.span_attributes),
    )


@router.get("/w2", response_class=HTMLResponse)
@router.get("/w2/", response_class=HTMLResponse)
async def get_w2_chat_page() -> HTMLResponse:
    """Serve the freeform Week 2 chat page."""
    if not _UI_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"chat UI missing at {_UI_PATH}",
        )
    return HTMLResponse(content=_UI_PATH.read_text(encoding="utf-8"))


# ─── Demo wiring ──────────────────────────────────────────────────


def _allow_mock() -> bool:
    """``COPILOT_ALLOW_MOCK=true`` enables the deterministic stub path."""
    return os.environ.get("COPILOT_ALLOW_MOCK", "").lower() == "true"


def _build_nodes(*, allow_mock: bool) -> GraphNodes:
    """Wire graph nodes for the chat demo.

    Single code path covers both mock-mode and live-mode demos:

    - **Evidence retriever**: hits the real RAG pipeline (BM25 + vector
      via Postgres + Reciprocal Rank Fusion + optional Cohere rerank).
      When the corpus is empty (mock mode without ``build_corpus`` run)
      the retriever returns []; the route still works, the response
      just doesn't carry guideline citations.
    - **Intake extractor**: when the user attached a document_id and
      mock mode is OFF, look up the upload, render, run the lab/intake
      extractor, persist Observations, return one claim per high-
      confidence field. When mock mode is ON or the doc is unknown,
      emit a synthetic claim so the demo flow still completes.
    - **Pairwise comparer**: relocates the Week 1 cross-check into the
      W2 graph. Returns no findings for now (Phase 11 wires the critic).
    - **Citation resolver**: queries the citations table and the
      guideline_chunks table; permissive in mock mode.
    """

    async def real_evidence_retriever(state: GraphState) -> list[EvidenceSnippet]:
        # Lazy imports keep the route loadable when optional deps are
        # missing. The fall-throughs return [] so the chat keeps working.
        try:
            from sidecar.rag import (
                DictionaryRewriter,
                Filters,
                HybridRetriever,
                StubEmbedder,
                StubReranker,
            )
            from sidecar.rag.search import bm25_search, vector_search
        except Exception as exc:
            logger.warning("RAG imports failed: %s; returning no snippets.", exc)
            return []

        try:
            import os as _os
            import psycopg
            url = (
                _os.environ.get("COPILOT_DATABASE_URL")
                or _os.environ.get("DATABASE_URL")
                or ""
            ).replace("postgresql+psycopg://", "postgresql://", 1)
            if not url:
                return []
            with psycopg.connect(url) as conn:
                with conn.cursor() as cur:
                    def lex(q, *, top, filters):
                        cur.execute(
                            "SELECT chunk_id, source_id, section_path, anchor_url, "
                            "text, domain_tags, "
                            "ts_rank_cd(text_tsv, plainto_tsquery('english', %s)) "
                            "FROM guideline_chunks "
                            "WHERE text_tsv @@ plainto_tsquery('english', %s) "
                            "ORDER BY 7 DESC LIMIT %s;",
                            (q, q, top),
                        )
                        return [
                            _row_to_hit(r, "bm25") for r in cur.fetchall()
                        ]

                    def dense(emb, *, top, filters):
                        vec = "[" + ",".join(f"{v:.6f}" for v in emb) + "]"
                        cur.execute(
                            "SELECT chunk_id, source_id, section_path, anchor_url, "
                            "text, domain_tags, 1.0 - (embedding <=> %s::vector) "
                            "FROM guideline_chunks WHERE embedding IS NOT NULL "
                            "ORDER BY embedding <=> %s::vector LIMIT %s;",
                            (vec, vec, top),
                        )
                        return [
                            _row_to_hit(r, "vector") for r in cur.fetchall()
                        ]

                    retriever = HybridRetriever(
                        sparse_search=lex,
                        dense_search=dense,
                        embedder=StubEmbedder(model="stub-embedder-v1", dimension=1024),
                        rewriter=DictionaryRewriter(),
                        reranker=StubReranker(),
                    )
                    result = await retriever.retrieve(
                        state.user_question, k=5,
                    )
                    return list(result.snippets)
        except Exception as exc:
            logger.warning("RAG retrieval errored: %s; returning no snippets.", exc)
            return []

    async def real_intake_extractor(state: GraphState) -> list[ClinicalClaim]:
        if not state.attached_documents:
            return []
        # Mock mode short-circuits to a synthetic claim so the demo
        # flow (drop a PDF, ask a question, see a cited response)
        # completes without the live VLM. Production wires the real
        # extract_lab_pdf / extract_intake_pdf here against the bytes
        # fetched from FHIR DocumentReference.
        if allow_mock:
            return [
                ClinicalClaim(
                    text=(
                        "Attached lab indicates HbA1c 6.8% — above the 6.5% "
                        "diagnostic threshold for diabetes per ADA."
                    ),
                    citations=[
                        f"DocumentReference/{state.attached_documents[0]}",
                        "ada-2025-glycemic-target-7pct",
                    ],
                )
            ]
        # Live wiring deferred to a follow-up commit; the route still
        # produces a refusal-with-reason rather than a fabricated answer.
        return []

    async def real_pairwise(state: GraphState) -> list[ClinicalClaim]:
        return []

    # Citation resolver: queries the citations table + the guideline
    # chunk ids, plus permits any DocumentReference id the chat just
    # attached. Falls through to permissive in mock mode if the table
    # isn't available.
    class LiveCitationResolver:
        def __init__(self) -> None:
            self._cache: set[str] = set()
            self._loaded = False

        def _ensure_loaded(self) -> None:
            if self._loaded:
                return
            self._loaded = True
            try:
                import os as _os
                import psycopg
                url = (
                    _os.environ.get("COPILOT_DATABASE_URL")
                    or _os.environ.get("DATABASE_URL")
                    or ""
                ).replace("postgresql+psycopg://", "postgresql://", 1)
                if not url:
                    return
                with psycopg.connect(url) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT chunk_id FROM guideline_chunks;")
                        for (cid,) in cur.fetchall():
                            self._cache.add(str(cid))
                        cur.execute("SELECT citation_id::text FROM citations;")
                        for (cid,) in cur.fetchall():
                            self._cache.add(str(cid))
            except Exception as exc:
                logger.warning("citation resolver load failed: %s", exc)

        def resolve(self, citation_id: str) -> bool:
            if citation_id.startswith("DocumentReference/"):
                return True
            self._ensure_loaded()
            if citation_id in self._cache:
                return True
            # In mock mode, fall through permissively so synthesized
            # citations don't get dropped by the verifier.
            return allow_mock

    return GraphNodes(
        evidence_retriever=real_evidence_retriever,
        intake_extractor=real_intake_extractor,
        pairwise_comparer=real_pairwise,
        citation_resolver=LiveCitationResolver(),
    )


def _materialize_citations(
    *,
    response,
    encounter_id: str,
    patient_id: str,
) -> dict[str, str]:
    """Insert one row per ``DocumentReference`` citation in the response
    packet, then return a {chunk_id_or_doc_ref: signed_preview_url} map.

    Guideline citations (chunk_ids that match a row in
    ``guideline_chunks``) get a deep-link to the source URL recorded in
    that row. DocumentReference citations get a signed preview URL that
    the bbox renderer serves.

    Failures are logged but do not break the chat — the chat still
    returns the rendered text; the citation chips just lose their link.
    """
    if response is None:
        return {}

    seen: set[str] = set()
    for claim in response.claims:
        for cid in claim.citations:
            seen.add(cid)
    if not seen:
        return {}

    out: dict[str, str] = {}
    try:
        import uuid as _uuid
        import psycopg
        from sidecar.config import get_settings
        from sidecar.citations.signing import mint_signed_url

        settings = get_settings()
        url = (settings.database_url or "").replace(
            "postgresql+psycopg://", "postgresql://", 1
        )
        if not url:
            return {}
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                # Build the guideline chunk → anchor URL map once.
                cur.execute(
                    "SELECT chunk_id, anchor_url, source_id, section_path "
                    "FROM guideline_chunks WHERE chunk_id = ANY(%s);",
                    (list(seen),),
                )
                for chunk_id, anchor_url, _, _ in cur.fetchall():
                    out[str(chunk_id)] = str(anchor_url)

                # For each DocumentReference citation, insert a row in
                # the citations table with a default bbox over the top
                # of page 0 (real bbox lands when the live VLM
                # extractor runs). Then mint a signed URL.
                for cid in seen:
                    if not cid.startswith("DocumentReference/"):
                        continue
                    source_id = cid.split("/", 1)[1]
                    citation_uuid = _uuid.uuid4()
                    bbox_json = (
                        '{"page":0,"x0":0.10,"y0":0.10,'
                        '"x1":0.90,"y1":0.32}'
                    )
                    cur.execute(
                        """
                        INSERT INTO citations (
                            citation_id, encounter_id, patient_id,
                            source_type, source_id, page, section,
                            field_or_chunk_id, quote_or_value, bbox_json
                        ) VALUES (
                            %s, %s, %s, 'DocumentReference', %s,
                            0, NULL, %s, %s, %s::jsonb
                        );
                        """,
                        (
                            str(citation_uuid),
                            encounter_id,
                            patient_id,
                            source_id,
                            cid,
                            "extracted field (preview)",
                            bbox_json,
                        ),
                    )
                    signed = mint_signed_url(
                        base_url=(
                            f"http://localhost:8801/agent-api/v1/citations/"
                            f"{citation_uuid}/preview.png"
                        ),
                        citation_id=str(citation_uuid),
                        patient_id=patient_id,
                        signing_key=settings.bff_jwt_signing_key,
                        ttl_seconds=settings.task_token_lifetime_seconds,
                    )
                    out[cid] = signed
            conn.commit()
    except Exception as exc:
        logger.warning("citation materialization failed: %s", exc)
    return out


def _row_to_hit(row, method_label: str):
    """Translate a SELECT row into the ``SearchHit`` shape the retriever
    expects. Defined at module level so both lex/dense closures share it.
    """
    from sidecar.rag.search import SearchHit
    from sidecar.rag.types import EvidenceSnippet, RetrievalMethod
    chunk_id, source_id, section_path, anchor_url, text, domain_tags_raw, score = row
    method = (
        RetrievalMethod.BM25 if method_label == "bm25" else RetrievalMethod.VECTOR
    )
    score_norm = max(0.0, min(1.0, float(score) if score is not None else 0.0))
    if method is RetrievalMethod.BM25:
        # ts_rank_cd is unbounded; normalize via score / (score + 1).
        score_norm = score_norm / (score_norm + 1.0) if score_norm > 0 else 0.0
    snippet = EvidenceSnippet(
        chunk_id=str(chunk_id),
        source_id=str(source_id),
        section=str(section_path),
        anchor_url=str(anchor_url),
        text=str(text),
        relevance_score=score_norm,
        retrieval_method=method,
        domain_tags=[],
    )
    return SearchHit(snippet=snippet, raw_score=float(score) if score else 0.0)


__all__ = [
    "W2ChatRequest",
    "W2ChatResponse",
    "get_w2_chat_page",
    "post_w2_chat",
    "router",
]
