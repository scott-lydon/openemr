"""Deterministic supervisor preflight + versioned LLM judge fallback.

Two-stage routing:

1. **Preflight** — pure function over ``GraphState``. Inspects
   ``user_question``, ``attached_documents``, and ``intent_tags`` and
   tries to name the worker sequence using deterministic rules. Returns
   ``(sequence, DecisionPath.PREFLIGHT)`` when a rule fires; returns
   ``None`` when no rule matches.
2. **Judge** — LLM call when preflight returned ``None``. Sends the
   user question and a fixed set of "decision options" to a Hierarchical
   Aggregator (Haiku-class) model with a versioned prompt. Returns the
   selected sequence or falls through to ``DecisionPath.FALLBACK_DEFAULT``
   when the judge cannot map either.

Why deterministic preflight first:

- Most intents map to a small set of common shapes. A deterministic
  rule is faster, cheaper, and reproducible across runs. The unit
  tests assert determinism over 100 runs of the same input; the rule
  must fire identically every time.
- The judge is reserved for the long tail. Its prompt is versioned so
  a regression after an edit is attributable on the span.
- ``decision_path`` lands on the span unconditionally, so a dashboard
  panel "fraction of decisions on judge path" tells the operator
  whether preflight is degrading.

Worker sequences:

| Intent | Sequence |
|---|---|
| LAB_FOLLOWUP (document attached) | INTAKE_EXTRACTOR -> EVIDENCE_PACKET_BUILDER -> VERIFIER -> RESPONSE_FORMATTER |
| GUIDELINE_LOOKUP | EVIDENCE_RETRIEVER -> EVIDENCE_PACKET_BUILDER -> VERIFIER -> RESPONSE_FORMATTER |
| PAIRWISE_COMPARE | PAIRWISE_COMPARER -> EVIDENCE_PACKET_BUILDER -> VERIFIER -> RESPONSE_FORMATTER |
| CHART_REVIEW | EVIDENCE_RETRIEVER -> PAIRWISE_COMPARER -> EVIDENCE_PACKET_BUILDER -> VERIFIER -> RESPONSE_FORMATTER |
| (anything else) | judge picks; fallback refuses |
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final, Protocol

from sidecar.agents.w2.state import (
    DecisionPath,
    GraphState,
    IntentKind,
    WorkerName,
)


logger = logging.getLogger(__name__)


SUPERVISOR_JUDGE_PROMPT_VERSION: Final[str] = "supervisor.judge.v1"


# Patterns that drive the deterministic preflight. Each pattern maps to
# an intent kind. Multiple patterns can fire; the supervisor uses the
# first match in the listed order, so the order is significant.
_PREFLIGHT_PATTERNS: Final[tuple[tuple[re.Pattern[str], IntentKind], ...]] = (
    (
        re.compile(
            r"\b(contradict(s|ion)?|conflict|disagree|mismatch|inconsistenc)\b",
            re.IGNORECASE,
        ),
        IntentKind.PAIRWISE_COMPARE,
    ),
    (
        re.compile(
            r"\b(summari[sz]e|interpret|review)\b.*\b(lab|labs|result(s)?|panel|"
            r"hba1c|cholesterol|blood)\b",
            re.IGNORECASE,
        ),
        IntentKind.LAB_FOLLOWUP,
    ),
    (
        re.compile(
            r"\b(should I|recommend|guideline|screening|preventive|target|"
            r"goal|when to (start|stop|treat)|criteria)\b",
            re.IGNORECASE,
        ),
        IntentKind.GUIDELINE_LOOKUP,
    ),
    (
        re.compile(
            r"\b(chart|history|past|prior|previous|trend)\b",
            re.IGNORECASE,
        ),
        IntentKind.CHART_REVIEW,
    ),
)


@dataclass(frozen=True)
class SupervisorDecision:
    """Output of a supervisor invocation."""

    intent_kind: IntentKind
    worker_sequence: list[WorkerName]
    decision_path: DecisionPath


def preflight(state: GraphState) -> SupervisorDecision | None:
    """Deterministic intent routing. Returns ``None`` if no rule fires.

    The pattern match runs against ``user_question`` first; an attached
    document upgrades the intent to ``LAB_FOLLOWUP`` even if the
    question text alone would have routed to GUIDELINE_LOOKUP, because
    the document is what the user clearly wants summarized.
    """
    if state.attached_documents:
        return SupervisorDecision(
            intent_kind=IntentKind.LAB_FOLLOWUP,
            worker_sequence=[
                WorkerName.INTAKE_EXTRACTOR,
                WorkerName.EVIDENCE_PACKET_BUILDER,
                WorkerName.VERIFIER,
                WorkerName.RESPONSE_FORMATTER,
            ],
            decision_path=DecisionPath.PREFLIGHT,
        )

    question = state.user_question.strip()
    if not question:
        return None

    for pattern, kind in _PREFLIGHT_PATTERNS:
        if pattern.search(question):
            return SupervisorDecision(
                intent_kind=kind,
                worker_sequence=_sequence_for(kind),
                decision_path=DecisionPath.PREFLIGHT,
            )
    return None


def _sequence_for(kind: IntentKind) -> list[WorkerName]:
    """Worker sequence per intent kind. Closed mapping; missing intent
    raises so the table stays exhaustive."""
    if kind is IntentKind.LAB_FOLLOWUP:
        return [
            WorkerName.INTAKE_EXTRACTOR,
            WorkerName.EVIDENCE_PACKET_BUILDER,
            WorkerName.VERIFIER,
            WorkerName.RESPONSE_FORMATTER,
        ]
    if kind is IntentKind.GUIDELINE_LOOKUP:
        return [
            WorkerName.EVIDENCE_RETRIEVER,
            WorkerName.EVIDENCE_PACKET_BUILDER,
            WorkerName.VERIFIER,
            WorkerName.RESPONSE_FORMATTER,
        ]
    if kind is IntentKind.PAIRWISE_COMPARE:
        return [
            WorkerName.PAIRWISE_COMPARER,
            WorkerName.EVIDENCE_PACKET_BUILDER,
            WorkerName.VERIFIER,
            WorkerName.RESPONSE_FORMATTER,
        ]
    if kind is IntentKind.CHART_REVIEW:
        return [
            WorkerName.EVIDENCE_RETRIEVER,
            WorkerName.PAIRWISE_COMPARER,
            WorkerName.EVIDENCE_PACKET_BUILDER,
            WorkerName.VERIFIER,
            WorkerName.RESPONSE_FORMATTER,
        ]
    raise ValueError(f"_sequence_for has no mapping for {kind}")


# ─── Judge fallback ──────────────────────────────────────────────────


class SupervisorJudge(Protocol):
    """Protocol for the LLM judge.

    The judge sees the user question and a list of decision options
    (the closed enum's values plus ``unknown``). It returns the chosen
    intent. A pure-text protocol means the production implementation
    can be OpenAI, Anthropic, or anything else.
    """

    async def judge(
        self, *, user_question: str, prompt_version: str
    ) -> IntentKind:
        ...


@dataclass
class StubSupervisorJudge:
    """Deterministic test substitute.

    The fixture map is keyed on the user question. A missing question
    raises ``KeyError`` so a forgotten test fixture fails loudly.
    """

    answers: dict[str, IntentKind]
    invocations: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.invocations is None:
            self.invocations = []

    async def judge(
        self, *, user_question: str, prompt_version: str
    ) -> IntentKind:
        self.invocations.append(user_question)
        if user_question not in self.answers:
            raise KeyError(
                f"StubSupervisorJudge: no answer for {user_question!r}; "
                "register one before the test."
            )
        return self.answers[user_question]


async def supervise(
    state: GraphState,
    *,
    judge: SupervisorJudge | None = None,
) -> SupervisorDecision:
    """End-to-end supervisor: preflight first, judge fallback, then default.

    Always returns a decision; the worst case is
    ``DecisionPath.FALLBACK_DEFAULT`` with an empty sequence (the
    response formatter then produces a refusal-with-reason rather than
    fabricating).
    """
    deterministic = preflight(state)
    if deterministic is not None:
        return deterministic

    if judge is None:
        return SupervisorDecision(
            intent_kind=IntentKind.UNKNOWN,
            worker_sequence=[],
            decision_path=DecisionPath.FALLBACK_DEFAULT,
        )

    try:
        kind = await judge.judge(
            user_question=state.user_question,
            prompt_version=SUPERVISOR_JUDGE_PROMPT_VERSION,
        )
    except Exception as exc:
        logger.warning(
            "supervisor judge raised %s; falling back to default",
            exc,
        )
        return SupervisorDecision(
            intent_kind=IntentKind.UNKNOWN,
            worker_sequence=[],
            decision_path=DecisionPath.FALLBACK_DEFAULT,
        )

    if kind is IntentKind.UNKNOWN:
        return SupervisorDecision(
            intent_kind=IntentKind.UNKNOWN,
            worker_sequence=[],
            decision_path=DecisionPath.FALLBACK_DEFAULT,
        )

    return SupervisorDecision(
        intent_kind=kind,
        worker_sequence=_sequence_for(kind),
        decision_path=DecisionPath.JUDGE,
    )


__all__ = [
    "SUPERVISOR_JUDGE_PROMPT_VERSION",
    "StubSupervisorJudge",
    "SupervisorDecision",
    "SupervisorJudge",
    "preflight",
    "supervise",
]
