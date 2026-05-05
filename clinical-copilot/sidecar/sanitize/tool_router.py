"""Layer 6 of the sanitization stack: surprising-tool-call router.

When the planner LLM proposes a tool call, the router runs a small
classifier (a Hierarchical Aggregator (Haiku-class) LLM with a
versioned prompt, or a deterministic policy when no LLM is configured)
to ask: "Given the user's question and the tools the planner has been
authorized to call, is this proposed tool call expected?"

A tool call that lands inside the authorized set passes through. A
surprising tool call (one the user did not ask for, e.g. exfiltrating
a chart row when the user asked for a guideline lookup) is refused
and the offending step is logged to the trace as
``sanitize.layer6.blocked=true``.

Two implementations:

- ``DeterministicToolRouter`` — pure-Python policy. Each tool name has
  a list of authorized intent kinds; the router's allow-list rejects
  anything outside the table. Cheap, no network, no model.
- ``LlmToolRouter`` — Phase 11 expansion that swaps in an LLM
  classifier for the tail. Out of scope here; the protocol seam is in
  place for the future.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Mapping, Protocol


logger = logging.getLogger(__name__)


TOOL_ROUTER_PROMPT_VERSION: Final[str] = "tool_router.v1"


class ToolName(str, Enum):
    """Closed set of tool names the planner can request.

    Adding a new tool requires adding to this enum AND to the
    deterministic router's allowlist. Closed enum makes the review
    surface explicit.
    """

    SEARCH_GUIDELINES = "search_guidelines"
    READ_CHART_OBSERVATIONS = "read_chart_observations"
    READ_CHART_MEDICATIONS = "read_chart_medications"
    READ_CHART_ALLERGIES = "read_chart_allergies"
    READ_CHART_CONDITIONS = "read_chart_conditions"
    READ_DOCUMENT_EXTRACTION = "read_document_extraction"


@dataclass(frozen=True)
class ToolCallRequest:
    """A planner-proposed tool call awaiting routing approval."""

    tool_name: ToolName
    intent_kind: str  # IntentKind.value; loose for the Layer 6 boundary
    has_attached_document: bool


@dataclass(frozen=True)
class ToolRouterDecision:
    """Outcome of the router."""

    allowed: bool
    reason: str
    prompt_version: str


class ToolRouter(Protocol):
    def route(self, request: ToolCallRequest) -> ToolRouterDecision:
        ...


# Default allowlist. Each tool maps to the set of intent kinds where
# calling it is expected. ``"*"`` is the wildcard meaning "always
# allowed".
_DEFAULT_ALLOWLIST: Final[Mapping[ToolName, frozenset[str]]] = {
    ToolName.SEARCH_GUIDELINES: frozenset(
        {"guideline_lookup", "lab_followup", "chart_review", "pairwise_compare"}
    ),
    ToolName.READ_CHART_OBSERVATIONS: frozenset(
        {"lab_followup", "chart_review", "pairwise_compare"}
    ),
    ToolName.READ_CHART_MEDICATIONS: frozenset(
        {"chart_review", "pairwise_compare"}
    ),
    ToolName.READ_CHART_ALLERGIES: frozenset(
        {"chart_review", "pairwise_compare", "lab_followup"}
    ),
    ToolName.READ_CHART_CONDITIONS: frozenset({"chart_review", "pairwise_compare"}),
    ToolName.READ_DOCUMENT_EXTRACTION: frozenset({"lab_followup"}),
}


@dataclass
class DeterministicToolRouter:
    """Policy-driven router. No model call required."""

    allowlist: Mapping[ToolName, frozenset[str]] = field(
        default_factory=lambda: dict(_DEFAULT_ALLOWLIST)
    )

    def route(self, request: ToolCallRequest) -> ToolRouterDecision:
        permitted = self.allowlist.get(request.tool_name)
        if permitted is None:
            return ToolRouterDecision(
                allowed=False,
                reason=(
                    f"tool_name={request.tool_name.value!r} is not in the "
                    "router's allowlist; refused at the boundary."
                ),
                prompt_version=TOOL_ROUTER_PROMPT_VERSION,
            )
        if request.intent_kind in permitted:
            return ToolRouterDecision(
                allowed=True,
                reason="intent in tool's allowed set",
                prompt_version=TOOL_ROUTER_PROMPT_VERSION,
            )
        # Special case: a tool that needs a document attached can be
        # allowed when the user actually attached one.
        if request.tool_name is ToolName.READ_DOCUMENT_EXTRACTION and request.has_attached_document:
            return ToolRouterDecision(
                allowed=True,
                reason="document attached; tool allowed",
                prompt_version=TOOL_ROUTER_PROMPT_VERSION,
            )
        return ToolRouterDecision(
            allowed=False,
            reason=(
                f"tool_name={request.tool_name.value!r} not authorized for "
                f"intent_kind={request.intent_kind!r}; refused."
            ),
            prompt_version=TOOL_ROUTER_PROMPT_VERSION,
        )


__all__ = [
    "DeterministicToolRouter",
    "TOOL_ROUTER_PROMPT_VERSION",
    "ToolCallRequest",
    "ToolName",
    "ToolRouter",
    "ToolRouterDecision",
]
