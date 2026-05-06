"""End-to-end tests for the follow-up handler.

Exercises the path the v2 feedback explicitly called out: the
``message`` field on ``ChatRequest`` driving the model input, with
prior turns from session memory threaded into the prompt.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from sidecar.agent.conversation import ConversationTurn
from sidecar.agent.follow_up import (
    FollowUpAnswer,
    FollowUpConfig,
    _build_user_prompt,
    _mock_follow_up_answer,
    _render_snapshot_compact,
    run_follow_up,
)
from sidecar.agent.pair_judge import MockProvider
from sidecar.audit import InMemoryAuditLog
from sidecar.config import Settings
from sidecar.snapshot import (
    Demographics,
    PatientSnapshot,
    Presenting,
    Problem,
    Procedure,
    Provenance,
)


def _settings() -> Settings:
    return Settings()


def _gout_snapshot() -> PatientSnapshot:
    return PatientSnapshot(
        patient_id="Patient/42",
        snapshot_version=datetime.now(tz=timezone.utc),
        demographics=Demographics(age=68, sex_at_birth="female"),
        active_problems=[
            Problem(
                id="Condition/1", label="Gout", icd10="M10.9",
                onset=date(2019, 6, 1),
                provenance=Provenance(table="problems", row_id=1,
                                       observed_at=date(2019, 6, 1)),
            ),
        ],
        presenting=Presenting(symptoms=["right great toe pain"], since="2 days"),
    )


def _colonoscopy_snapshot() -> PatientSnapshot:
    return PatientSnapshot(
        patient_id="Patient/99",
        snapshot_version=datetime.now(tz=timezone.utc),
        demographics=Demographics(age=63, sex_at_birth="male"),
        procedures=[
            Procedure(
                id="Procedure/3", label="Colonoscopy", cpt="45378",
                performed=date(2024, 9, 15), status="completed",
                provenance=Provenance(table="procedure_order", row_id=3,
                                       observed_at=date(2024, 9, 15)),
            ),
        ],
    )


def test_render_snapshot_compact_emits_provenance_for_each_finding() -> None:
    """Every finding line must carry a ``<row table=… row_id=…>`` block."""
    rendered = _render_snapshot_compact(_gout_snapshot())
    assert "[diagnosis] Gout" in rendered
    assert "<row table=problems row_id=1" in rendered
    assert "PRESENTING:" in rendered


def test_build_user_prompt_includes_prior_turns_and_question() -> None:
    """Prior turns precede the current question — recency on the snapshot."""
    prior = [
        ConversationTurn(
            role="user", content="any recent uric acid?",
            ts_unix=time.time(), purpose="follow_up_question",
        ),
        ConversationTurn(
            role="assistant", content="insufficient_data",
            ts_unix=time.time(), purpose="follow_up_question",
        ),
    ]
    prompt = _build_user_prompt(_gout_snapshot(), "what about her CRP?", prior)
    assert "CONVERSATION_SO_FAR:" in prompt
    assert "any recent uric acid?" in prompt
    assert "QUESTION: what about her CRP?" in prompt
    # Snapshot block last so recency bias works in our favour.
    assert prompt.index("PATIENT_SNAPSHOT:") > prompt.index("QUESTION:")


def test_build_user_prompt_omits_history_when_no_prior_turns() -> None:
    prompt = _build_user_prompt(_gout_snapshot(), "what about her CRP?", [])
    assert "CONVERSATION_SO_FAR:" not in prompt
    assert "QUESTION: what about her CRP?" in prompt


def test_mock_follow_up_answer_cites_gout_when_present() -> None:
    """The mock provider exists so eval and offline runs are deterministic."""
    prompt = _build_user_prompt(_gout_snapshot(), "is gout on her chart?", [])
    answer = _mock_follow_up_answer(prompt)
    assert isinstance(answer, FollowUpAnswer)
    assert "gout" in answer.answer.lower()
    assert answer.citations  # at least one citation
    assert answer.citations[0].table == "problems"
    assert answer.citations[0].row_id == "1"


def test_mock_follow_up_answer_cites_colonoscopy_when_present() -> None:
    prompt = _build_user_prompt(
        _colonoscopy_snapshot(), "when was her last colonoscopy?", []
    )
    answer = _mock_follow_up_answer(prompt)
    assert answer.verdict == "answered"
    assert answer.citations
    assert answer.citations[0].table == "procedure_order"


def test_mock_follow_up_returns_insufficient_data_when_chart_silent() -> None:
    """No matching row → verdict='insufficient_data' with an honest gap."""
    snap = PatientSnapshot(
        patient_id="Patient/7", snapshot_version=datetime.now(tz=timezone.utc),
        demographics=Demographics(),
    )
    prompt = _build_user_prompt(snap, "any thyroid labs?", [])
    answer = _mock_follow_up_answer(prompt)
    assert answer.verdict == "insufficient_data"
    assert answer.data_gaps
    assert answer.citations == []


async def test_run_follow_up_appends_audit_row() -> None:
    """Every follow-up turn must leave a row in the audit log."""
    audit = InMemoryAuditLog()
    cfg = FollowUpConfig(
        message="is gout on her chart?",
        prior_turns=[],
        user_id="dr.m",
        settings=_settings(),
        audit_log=audit,
        provider=MockProvider(),
    )
    response = await run_follow_up(_gout_snapshot(), cfg)
    stored = list(audit)
    assert len(stored) == 1
    audit_row = stored[0].entry  # StoredAuditEntry wraps the AuditEntry
    assert audit_row.user_id == "dr.m"
    assert audit_row.patient_id == "Patient/42"
    assert audit_row.purpose_of_use == "follow_up_question"
    assert response.verdict in {"answered", "answered_with_gaps", "insufficient_data"}


async def test_run_follow_up_rejects_empty_message() -> None:
    """A direct caller bypassing the endpoint must still get a clear error."""
    cfg = FollowUpConfig(
        message="   ",
        prior_turns=[],
        user_id="dr.m",
        settings=_settings(),
        audit_log=InMemoryAuditLog(),
        provider=MockProvider(),
    )
    with pytest.raises(ValueError, match="non-empty message"):
        await run_follow_up(_gout_snapshot(), cfg)


async def test_run_follow_up_threads_prior_turns_into_response_telemetry() -> None:
    """Telemetry must report how many prior turns participated."""
    prior = [
        ConversationTurn(
            role="user", content="last lab?",
            ts_unix=time.time(), purpose="follow_up_question",
        ),
        ConversationTurn(
            role="assistant", content="answered",
            ts_unix=time.time(), purpose="follow_up_question",
        ),
    ]
    cfg = FollowUpConfig(
        message="and her uric acid?",
        prior_turns=prior,
        user_id="dr.m",
        settings=_settings(),
        audit_log=InMemoryAuditLog(),
        provider=MockProvider(),
    )
    response = await run_follow_up(_gout_snapshot(), cfg)
    assert response.telemetry.get("follow_up_prior_turn_count") == 2
