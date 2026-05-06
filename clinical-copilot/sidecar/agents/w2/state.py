"""Graph state Data Transfer Objects (DTOs).

The Phase 5 graph passes a typed state through every node. Each node is
a pure function from state to state plus side-effect span attributes;
the state's shape is the contract every node honors.

Why a single state object rather than per-node arguments:

- The state is the unit the graph checkpointer persists. LangGraph's
  PostgresSaver checkpoint is one row per state. A flat call signature
  would not survive a process restart in the middle of an agent run.
- A single typed object lets the graph's edges read from and write to
  it without each node knowing the others' signatures.
- ``extra='forbid'`` on the state model means a node that accidentally
  writes a typo'd attribute fails at the boundary, not three nodes
  downstream.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from sidecar.rag.types import EvidenceSnippet


class IntentKind(str, Enum):
    """Closed set of intent shapes the supervisor preflight recognizes.

    A new intent shape requires a new code path; closed enum forces the
    review when adding one. ``UNKNOWN`` is the explicit fallback the
    supervisor uses when it cannot map the user's question to a known
    intent — the fallback then triggers the LLM judge.
    """

    LAB_FOLLOWUP = "lab_followup"
    GUIDELINE_LOOKUP = "guideline_lookup"
    CHART_REVIEW = "chart_review"
    PAIRWISE_COMPARE = "pairwise_compare"
    UNKNOWN = "unknown"


class WorkerName(str, Enum):
    """The named workers the supervisor can route to."""

    INTAKE_EXTRACTOR = "intake_extractor"
    EVIDENCE_RETRIEVER = "evidence_retriever"
    PAIRWISE_COMPARER = "pairwise_comparer"
    EVIDENCE_PACKET_BUILDER = "evidence_packet_builder"
    VERIFIER = "verifier"
    RESPONSE_FORMATTER = "response_formatter"
    CRITIC = "critic"


class DecisionPath(str, Enum):
    """Which decision path the supervisor took.

    ``PREFLIGHT`` — deterministic rule fired and named the worker
    sequence directly. Cheap and fast; covers every intent we have a
    rule for.

    ``JUDGE`` — preflight returned UNKNOWN; the LLM judge ran. The
    judge is a Hierarchical Aggregator (Haiku-class) model with a
    versioned prompt. The version is recorded on the span.

    ``FALLBACK_DEFAULT`` — both preflight and judge produced no usable
    decision. The agent refuses the request. Reaching this path is a
    bug, not a feature.
    """

    PREFLIGHT = "preflight"
    JUDGE = "judge"
    FALLBACK_DEFAULT = "fallback_default"


class ClinicalClaim(BaseModel):
    """One assertion the agent might surface to the clinician.

    Each claim must carry a non-empty ``citations`` list; a claim with
    no citations is dropped by the verifier. The list typically
    contains one citation, but multi-source claims (a guideline + a
    chart row that together prove the recommendation applies) carry
    more.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    text: str
    citations: list[str] = Field(min_length=1)


class ResponsePacket(BaseModel):
    """The final structured response the agent produces.

    The response formatter renders this into the user-facing message.
    The verifier writes its drop count and any sanitization metadata
    into ``trace_attributes`` so the dashboard can show what was filtered.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str
    claims: list[ClinicalClaim]
    refusal_reason: str | None = None
    trace_attributes: dict[str, object] = Field(default_factory=dict)


class GraphState(BaseModel):
    """Typed state passed through every graph node.

    Every node reads from (and may write to) this state. ``extra='forbid'``
    forces type-check errors when a node mistypes an attribute name,
    catching plumbing bugs before runtime.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    encounter_id: str
    user_id: str
    patient_id: str
    user_question: str
    purpose_of_use: str
    started_at: datetime

    intent_kind: IntentKind = IntentKind.UNKNOWN
    intent_tags: list[str] = Field(default_factory=list)
    decision_path: DecisionPath = DecisionPath.PREFLIGHT
    worker_sequence: list[WorkerName] = Field(default_factory=list)

    attached_documents: list[str] = Field(default_factory=list)
    snippets: list[EvidenceSnippet] = Field(default_factory=list)
    raw_claims: list[ClinicalClaim] = Field(default_factory=list)
    verified_claims: list[ClinicalClaim] = Field(default_factory=list)
    response: ResponsePacket | None = None

    span_attributes: dict[str, object] = Field(default_factory=dict)


__all__ = [
    "ClinicalClaim",
    "DecisionPath",
    "GraphState",
    "IntentKind",
    "ResponsePacket",
    "WorkerName",
]
