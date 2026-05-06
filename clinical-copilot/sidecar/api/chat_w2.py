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
    return W2ChatResponse(
        rendered_text=result.rendered_text,
        decision_path=result.state.decision_path.value,
        intent_kind=result.state.intent_kind.value,
        worker_sequence=[w.value for w in result.state.worker_sequence],
        refused=bool(response and response.refusal_reason),
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

    In mock mode every worker is a deterministic stub, so the chat UI
    can be exercised end-to-end without API keys. Production replaces
    each stub with the real worker (RAG retriever, lab extractor,
    pairwise comparer).
    """
    if not allow_mock:
        # Production path. The wiring is left as a clear hand-off seam:
        # an operator running the chat live binds the real workers
        # here. For now the route refuses rather than silently producing
        # a mock answer in non-mock mode.
        raise HTTPException(
            status_code=503,
            detail={
                "error": "w2_chat_live_mode_not_wired",
                "message": (
                    "Live W2 chat requires the production graph nodes "
                    "(VLM extractor, real RAG retriever, real verifier) "
                    "to be wired in sidecar.api.chat_w2._build_nodes. "
                    "Set COPILOT_ALLOW_MOCK=true to use the demo stubs."
                ),
            },
        )

    async def stub_retriever(state: GraphState) -> list[EvidenceSnippet]:
        # One canned snippet so guideline_lookup intents have something
        # cited to surface. Stable across runs.
        return [
            EvidenceSnippet(
                chunk_id="ada-2025-glycemic-targets-001",
                source_id="ADA-Standards-of-Care-2025",
                section="Standards > Glycemic Targets",
                anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
                text=(
                    "ADA recommends a target HbA1c below 7% for most "
                    "non-pregnant adults with type 2 diabetes."
                ),
                relevance_score=0.92,
                retrieval_method=RetrievalMethod.RERANK,
                domain_tags=[],
            ),
        ]

    async def stub_intake_extractor(state: GraphState) -> list[ClinicalClaim]:
        if not state.attached_documents:
            return []
        return [
            ClinicalClaim(
                text=(
                    "Attached document indicates HbA1c 6.8% (above the 6.5% "
                    "diabetes diagnostic threshold)."
                ),
                citations=[f"DocumentReference/{state.attached_documents[0]}"],
            )
        ]

    async def stub_pairwise(state: GraphState) -> list[ClinicalClaim]:
        # The stub simulates a contradiction-check finding nothing.
        return []

    # Resolver accepts the canned guideline chunk + any DocumentReference id
    # the user actually uploaded. A real deployment binds to the citations
    # table; the stub is good enough for the freeform demo.
    known_ids = {"ada-2025-glycemic-targets-001"}
    if False:
        # Phase 11 critic could run here when wired.
        pass

    class PermissiveResolver:
        def resolve(self, citation_id: str) -> bool:
            # Permit anything that is either a known guideline chunk or
            # a DocumentReference reference the user just uploaded.
            return citation_id.startswith("DocumentReference/") or citation_id in known_ids

    return GraphNodes(
        evidence_retriever=stub_retriever,
        intake_extractor=stub_intake_extractor,
        pairwise_comparer=stub_pairwise,
        citation_resolver=PermissiveResolver(),
    )


__all__ = [
    "W2ChatRequest",
    "W2ChatResponse",
    "get_w2_chat_page",
    "post_w2_chat",
    "router",
]
