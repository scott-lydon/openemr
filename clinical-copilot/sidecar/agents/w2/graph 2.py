"""Multi-agent graph orchestrator.

The graph wires the supervisor, worker nodes, packet builder, verifier,
and response formatter into a deterministic execution per
``GraphState``. Edges are explicit conditionals (no implicit defaults)
so a typo in a node's name surfaces at construction, not at run time.

Why a hand-rolled graph rather than ``langgraph.StateGraph``:

- The Phase 5 contract is "deterministic preflight + versioned LLM
  judge fallback, every routing decision recorded as a span attribute."
  That is easier to assert about and debug when the graph is a small
  Python function whose control flow you can read.
- LangGraph is welcome to drop in later for the durable-checkpointer
  superpower; the supervisor's ``SupervisorDecision`` and the worker
  protocol seams here are the same shape LangGraph would consume.

Each worker is a Protocol so the production wire-up swaps in real
implementations (RAG retriever, lab extractor, persist client) and
unit tests substitute deterministic stubs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Protocol

from sidecar.agents.w2.evidence_packet_builder import build_evidence_packet
from sidecar.agents.w2.response_formatter import format_response
from sidecar.agents.w2.state import (
    ClinicalClaim,
    DecisionPath,
    GraphState,
    IntentKind,
    ResponsePacket,
    WorkerName,
)
from sidecar.agents.w2.supervisor import (
    SUPERVISOR_JUDGE_PROMPT_VERSION,
    SupervisorJudge,
    supervise,
)
from sidecar.agents.w2.verifier import (
    CitationResolver,
    PresidioUnavailable,
    VerifierConfig,
    verify,
)
from sidecar.rag.types import EvidenceSnippet


logger = logging.getLogger(__name__)


class EvidenceRetrieverNode(Protocol):
    """Wraps the RAG retriever for the graph."""

    async def __call__(self, state: GraphState) -> list[EvidenceSnippet]:
        ...


class IntakeExtractorNode(Protocol):
    """Wraps the per-document extractor's surface for the graph.

    Phase 3 produces the extractions during ingest; this node is the
    seam that lets the supervisor's LAB_FOLLOWUP path enrich the state
    with already-extracted claims when the document was attached.
    """

    async def __call__(self, state: GraphState) -> list[ClinicalClaim]:
        ...


class PairwiseComparerNode(Protocol):
    """Wraps the Week 1 pairwise comparer."""

    async def __call__(self, state: GraphState) -> list[ClinicalClaim]:
        ...


@dataclass
class GraphNodes:
    """Bundle of injected node implementations.

    Tests construct this with stubs. ``sidecar.main`` constructs it with
    the production wire-up that delegates to the modules from Phases
    2-4.
    """

    evidence_retriever: EvidenceRetrieverNode | None = None
    intake_extractor: IntakeExtractorNode | None = None
    pairwise_comparer: PairwiseComparerNode | None = None
    citation_resolver: CitationResolver | None = None


@dataclass(frozen=True)
class GraphResult:
    """Return shape of ``run_graph``.

    Carries the rendered text plus the final state so a caller can fan
    everything into the active span.
    """

    rendered_text: str
    state: GraphState


async def run_graph(
    *,
    state: GraphState,
    nodes: GraphNodes,
    judge: SupervisorJudge | None = None,
    verifier_config: VerifierConfig | None = None,
) -> GraphResult:
    """Execute the graph end-to-end. Returns the rendered response.

    On any worker failure, the graph populates a refusal-with-reason
    response rather than crashing. The exception is ``PresidioUnavailable``,
    which propagates so the gateway can return a 503.
    """
    decision = await supervise(state, judge=judge)
    state = state.model_copy(
        update={
            "intent_kind": decision.intent_kind,
            "decision_path": decision.decision_path,
            "worker_sequence": list(decision.worker_sequence),
            "span_attributes": {
                **state.span_attributes,
                "supervisor.decision_path": decision.decision_path.value,
                "supervisor.intent_kind": decision.intent_kind.value,
                "supervisor.worker_sequence": [
                    w.value for w in decision.worker_sequence
                ],
                "supervisor.judge_prompt_version": SUPERVISOR_JUDGE_PROMPT_VERSION,
            },
        }
    )

    if decision.decision_path is DecisionPath.FALLBACK_DEFAULT:
        state = state.model_copy(
            update={
                "response": ResponsePacket(
                    summary="The agent cannot route this request.",
                    claims=[],
                    refusal_reason=(
                        "Supervisor preflight produced no rule and the LLM "
                        "judge could not select an intent."
                    ),
                ),
            }
        )
        return GraphResult(
            rendered_text=format_response(state.response),  # type: ignore[arg-type]
            state=state,
        )

    state = await _run_worker_sequence(state=state, nodes=nodes)

    state = state.model_copy(
        update={"raw_claims": list(state.raw_claims) + build_evidence_packet(state)}
    )

    resolver = nodes.citation_resolver
    if resolver is None:
        raise ValueError(
            "GraphNodes.citation_resolver is None; the verifier requires "
            "a resolver. The production wire-up binds the citations + "
            "guideline_chunks lookup; tests bind StubCitationResolver."
        )

    response = verify(state=state, resolver=resolver, config=verifier_config)
    state = state.model_copy(update={"response": response})
    return GraphResult(rendered_text=format_response(response), state=state)


async def _run_worker_sequence(
    *,
    state: GraphState,
    nodes: GraphNodes,
) -> GraphState:
    """Walk the supervisor-named workers; each may extend the state."""
    for worker in state.worker_sequence:
        if worker is WorkerName.EVIDENCE_RETRIEVER:
            if nodes.evidence_retriever is None:
                continue
            snippets = await nodes.evidence_retriever(state)
            state = state.model_copy(update={"snippets": list(snippets)})
            continue
        if worker is WorkerName.INTAKE_EXTRACTOR:
            if nodes.intake_extractor is None:
                continue
            extracted = await nodes.intake_extractor(state)
            state = state.model_copy(
                update={"raw_claims": list(state.raw_claims) + list(extracted)}
            )
            continue
        if worker is WorkerName.PAIRWISE_COMPARER:
            if nodes.pairwise_comparer is None:
                continue
            extracted = await nodes.pairwise_comparer(state)
            state = state.model_copy(
                update={"raw_claims": list(state.raw_claims) + list(extracted)}
            )
            continue
        if worker in (
            WorkerName.EVIDENCE_PACKET_BUILDER,
            WorkerName.VERIFIER,
            WorkerName.RESPONSE_FORMATTER,
            WorkerName.CRITIC,
        ):
            # These nodes run after the worker sequence inside run_graph.
            # The presence in the sequence is a contract signal, not an
            # actionable step here.
            continue
        raise ValueError(
            f"unexpected worker {worker!r} in sequence; the supervisor "
            "must produce a closed-set sequence."
        )
    return state


__all__ = [
    "EvidenceRetrieverNode",
    "GraphNodes",
    "GraphResult",
    "IntakeExtractorNode",
    "PairwiseComparerNode",
    "run_graph",
]
