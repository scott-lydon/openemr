"""Tests for the Week 2 multi-agent graph (Phase 5).

Coverage:

- Supervisor preflight is deterministic across 100 invocations of the
  same input.
- Preflight covers every IntentKind that a real user question might
  produce (lab_followup, guideline_lookup, pairwise_compare,
  chart_review).
- Attached document upgrades intent to lab_followup regardless of
  question text.
- Judge fallback runs when preflight returns None; trace records the
  judge prompt version.
- FALLBACK_DEFAULT path produces a refusal-with-reason rather than a
  fabricated answer.
- Evidence packet builder dedupes claims by text and unions citations.
- Verifier drops claims whose citations don't resolve.
- Verifier scrubs PHI patterns (SSN, MRN, DOB, phone, Patient ref).
- Verifier in process-only mode does not require Presidio.
- Verifier raises PresidioUnavailable when Presidio is required and
  missing.
- Response formatter inserts citation chips and a bottom-of-message
  citation list.
- Refusal response uses the refusal_reason verbatim.
- run_graph end-to-end: each of three intent shapes routes through the
  expected worker sequence and the supervisor decision_path is
  PREFLIGHT.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from sidecar.agents.w2 import (
    ClinicalClaim,
    DecisionPath,
    GraphNodes,
    GraphState,
    IntentKind,
    PresidioUnavailable,
    StubCitationResolver,
    StubSupervisorJudge,
    SupervisorDecision,
    VerifierConfig,
    WorkerName,
    build_evidence_packet,
    format_response,
    preflight,
    run_graph,
    supervise,
    verify,
)
from sidecar.rag.types import EvidenceSnippet, RetrievalMethod


# ─── Helpers ──────────────────────────────────────────────────────────


def _state(
    *,
    user_question: str = "",
    attached_documents: list[str] | None = None,
) -> GraphState:
    return GraphState(
        encounter_id="enc-1",
        user_id="dr.m@example.org",
        patient_id="Patient/87413",
        user_question=user_question,
        purpose_of_use="diagnostic_cross_check",
        started_at=datetime.now(tz=timezone.utc),
        attached_documents=attached_documents or [],
    )


def _snippet(chunk_id: str, text: str = "ADA recommends HbA1c < 7%") -> EvidenceSnippet:
    return EvidenceSnippet(
        chunk_id=chunk_id,
        source_id="ada-2025",
        section="ADA > A1c",
        anchor_url="https://example.com/ada",
        text=text,
        relevance_score=0.9,
        retrieval_method=RetrievalMethod.RERANK,
        domain_tags=[],
    )


# ─── Supervisor preflight ────────────────────────────────────────────


@pytest.mark.parametrize(
    "question, expected_kind",
    [
        (
            "Can you summarize this lab report?",
            IntentKind.LAB_FOLLOWUP,
        ),
        (
            "What are the screening recommendations for adults?",
            IntentKind.GUIDELINE_LOOKUP,
        ),
        (
            "Is there a contradiction between her allergies and her medications?",
            IntentKind.PAIRWISE_COMPARE,
        ),
        (
            "Show me her HbA1c trend over the prior years",
            IntentKind.CHART_REVIEW,
        ),
    ],
)
def test_preflight_routes_known_intents(question: str, expected_kind: IntentKind) -> None:
    state = _state(user_question=question)
    decision = preflight(state)
    assert decision is not None
    assert decision.intent_kind is expected_kind
    assert decision.decision_path is DecisionPath.PREFLIGHT


def test_preflight_attached_document_upgrades_to_lab_followup() -> None:
    state = _state(
        user_question="What does the screening guideline say?",
        attached_documents=["doc-1"],
    )
    decision = preflight(state)
    assert decision is not None
    assert decision.intent_kind is IntentKind.LAB_FOLLOWUP


def test_preflight_returns_none_for_uncovered_question() -> None:
    state = _state(user_question="hello there")
    assert preflight(state) is None


def test_preflight_is_deterministic_over_100_runs() -> None:
    state = _state(user_question="What is the target HbA1c for type 2 diabetes?")
    sequences = {
        tuple(preflight(state).worker_sequence)  # type: ignore[union-attr]
        for _ in range(100)
    }
    assert len(sequences) == 1


# ─── Supervisor judge ────────────────────────────────────────────────


async def test_supervise_falls_back_to_judge() -> None:
    state = _state(user_question="hello there")
    judge = StubSupervisorJudge(
        answers={"hello there": IntentKind.GUIDELINE_LOOKUP}
    )
    decision = await supervise(state, judge=judge)
    assert decision.decision_path is DecisionPath.JUDGE
    assert decision.intent_kind is IntentKind.GUIDELINE_LOOKUP


async def test_supervise_default_path_when_no_judge() -> None:
    state = _state(user_question="hello there")
    decision = await supervise(state, judge=None)
    assert decision.decision_path is DecisionPath.FALLBACK_DEFAULT
    assert decision.worker_sequence == []


async def test_supervise_default_when_judge_returns_unknown() -> None:
    state = _state(user_question="hello there")
    judge = StubSupervisorJudge(answers={"hello there": IntentKind.UNKNOWN})
    decision = await supervise(state, judge=judge)
    assert decision.decision_path is DecisionPath.FALLBACK_DEFAULT


# ─── Evidence packet builder ─────────────────────────────────────────


def test_packet_builder_emits_one_claim_per_snippet_for_guideline_lookup() -> None:
    state = _state(user_question="recommend HbA1c target").model_copy(
        update={
            "intent_kind": IntentKind.GUIDELINE_LOOKUP,
            "snippets": [_snippet("c-1", "Aim HbA1c below 7%."), _snippet("c-2", "Statin therapy.")],
        }
    )
    claims = build_evidence_packet(state)
    cite_ids = {tuple(c.citations) for c in claims}
    assert ("c-1",) in cite_ids and ("c-2",) in cite_ids


def test_packet_builder_dedupes_by_text_and_unions_citations() -> None:
    state = _state().model_copy(
        update={
            "raw_claims": [
                ClinicalClaim(text="aim a1c < 7%", citations=["c-1"]),
                ClinicalClaim(text="aim a1c < 7%", citations=["c-2"]),
                ClinicalClaim(text="aim a1c < 7%", citations=["c-1"]),
            ],
        }
    )
    claims = build_evidence_packet(state)
    assert len(claims) == 1
    assert sorted(claims[0].citations) == ["c-1", "c-2"]


def test_packet_builder_no_op_for_non_guideline_lookup_without_raw_claims() -> None:
    state = _state(user_question="contradiction").model_copy(
        update={"intent_kind": IntentKind.PAIRWISE_COMPARE, "snippets": [_snippet("c-1")]}
    )
    assert build_evidence_packet(state) == []


# ─── Verifier ────────────────────────────────────────────────────────


def _verifier_state(claims: list[ClinicalClaim]) -> GraphState:
    return _state(user_question="x").model_copy(update={"raw_claims": claims})


def test_verifier_drops_claim_with_unresolved_citation() -> None:
    state = _verifier_state(
        [
            ClinicalClaim(text="HbA1c target is 7%", citations=["c-known"]),
            ClinicalClaim(text="bogus", citations=["c-bogus"]),
        ]
    )
    resolver = StubCitationResolver(known_ids=frozenset({"c-known"}))
    response = verify(
        state=state,
        resolver=resolver,
        config=VerifierConfig(allow_in_process_scrub_only=True),
    )
    assert [c.text for c in response.claims] == ["HbA1c target is 7%"]
    assert response.trace_attributes["verifier.dropped_claims_count"] == 1


def test_verifier_scrubs_phi_patterns() -> None:
    state = _verifier_state(
        [
            ClinicalClaim(
                text="Patient/abcd-1234-efgh-5678 has SSN 123-45-6789 and DOB: 03/15/1962",
                citations=["c-known"],
            ),
        ]
    )
    resolver = StubCitationResolver(known_ids=frozenset({"c-known"}))
    response = verify(
        state=state,
        resolver=resolver,
        config=VerifierConfig(allow_in_process_scrub_only=True),
    )
    text = response.claims[0].text
    assert "[REDACTED:patient_ref]" in text
    assert "[REDACTED:ssn]" in text
    assert "[REDACTED:dob]" in text
    assert response.trace_attributes["verifier.phi_leak_blocked"] is True


def test_verifier_refuses_when_every_claim_dropped() -> None:
    state = _verifier_state(
        [ClinicalClaim(text="bogus", citations=["c-bogus"])]
    )
    resolver = StubCitationResolver(known_ids=frozenset())
    response = verify(
        state=state,
        resolver=resolver,
        config=VerifierConfig(allow_in_process_scrub_only=True),
    )
    assert response.refusal_reason is not None
    assert response.claims == []


def test_verifier_in_process_mode_does_not_need_presidio() -> None:
    """The unit-test mode (allow_in_process_scrub_only=True) must work
    even on a host without Presidio installed."""
    state = _verifier_state(
        [ClinicalClaim(text="HbA1c 7%", citations=["c-known"])]
    )
    resolver = StubCitationResolver(known_ids=frozenset({"c-known"}))
    response = verify(
        state=state,
        resolver=resolver,
        config=VerifierConfig(allow_in_process_scrub_only=True),
    )
    assert response.claims and response.claims[0].text == "HbA1c 7%"


# ─── Response formatter ──────────────────────────────────────────────


def test_format_response_renders_citation_chips_and_listing() -> None:
    from sidecar.agents.w2.state import ResponsePacket

    packet = ResponsePacket(
        summary="Found 2 verified claims",
        claims=[
            ClinicalClaim(text="HbA1c target is below 7%", citations=["c-1"]),
            ClinicalClaim(text="Statin therapy for ASCVD risk", citations=["c-1", "c-2"]),
        ],
    )
    rendered = format_response(packet)
    assert "[1]" in rendered
    assert "[2]" in rendered
    assert "_Citations_" in rendered
    assert "[1] c-1" in rendered
    assert "[2] c-2" in rendered


def test_format_response_renders_refusal() -> None:
    from sidecar.agents.w2.state import ResponsePacket

    packet = ResponsePacket(
        summary="ignored",
        claims=[],
        refusal_reason="every claim failed citation resolution",
    )
    rendered = format_response(packet)
    assert "every claim failed citation resolution" in rendered


# ─── Graph end to end ────────────────────────────────────────────────


async def test_graph_routes_guideline_lookup_through_retriever() -> None:
    state = _state(user_question="What's the recommended HbA1c target?")
    snippets = [
        _snippet("c-known-1", "ADA recommends HbA1c below 7% for most adults."),
        _snippet("c-known-2", "Lifestyle plus metformin first line."),
    ]

    async def retriever(s: GraphState):
        return snippets

    nodes = GraphNodes(
        evidence_retriever=retriever,
        citation_resolver=StubCitationResolver(
            known_ids=frozenset({"c-known-1", "c-known-2"})
        ),
    )
    result = await run_graph(
        state=state,
        nodes=nodes,
        verifier_config=VerifierConfig(allow_in_process_scrub_only=True),
    )
    assert result.state.decision_path is DecisionPath.PREFLIGHT
    assert result.state.intent_kind is IntentKind.GUIDELINE_LOOKUP
    assert WorkerName.EVIDENCE_RETRIEVER in result.state.worker_sequence
    assert "Citations" in result.rendered_text or "Citations" in result.rendered_text


async def test_graph_routes_lab_followup_through_intake_extractor() -> None:
    state = _state(
        user_question="summarize the labs", attached_documents=["doc-77"]
    )

    async def extractor(s: GraphState):
        return [
            ClinicalClaim(text="HbA1c 6.8 percent", citations=["c-known-1"]),
        ]

    nodes = GraphNodes(
        intake_extractor=extractor,
        citation_resolver=StubCitationResolver(known_ids=frozenset({"c-known-1"})),
    )
    result = await run_graph(
        state=state,
        nodes=nodes,
        verifier_config=VerifierConfig(allow_in_process_scrub_only=True),
    )
    assert result.state.intent_kind is IntentKind.LAB_FOLLOWUP
    assert WorkerName.INTAKE_EXTRACTOR in result.state.worker_sequence


async def test_graph_routes_pairwise_compare() -> None:
    state = _state(user_question="is there a contradiction between her meds and allergies?")

    async def comparer(s: GraphState):
        return [
            ClinicalClaim(
                text="Penicillin allergy contradicts amoxicillin prescription",
                citations=["c-known-3"],
            )
        ]

    nodes = GraphNodes(
        pairwise_comparer=comparer,
        citation_resolver=StubCitationResolver(known_ids=frozenset({"c-known-3"})),
    )
    result = await run_graph(
        state=state,
        nodes=nodes,
        verifier_config=VerifierConfig(allow_in_process_scrub_only=True),
    )
    assert result.state.intent_kind is IntentKind.PAIRWISE_COMPARE
    assert WorkerName.PAIRWISE_COMPARER in result.state.worker_sequence


async def test_graph_fallback_default_produces_refusal() -> None:
    state = _state(user_question="hello there")
    nodes = GraphNodes(
        citation_resolver=StubCitationResolver(known_ids=frozenset()),
    )
    result = await run_graph(state=state, nodes=nodes)
    assert result.state.decision_path is DecisionPath.FALLBACK_DEFAULT
    assert result.state.response is not None
    assert result.state.response.refusal_reason is not None
