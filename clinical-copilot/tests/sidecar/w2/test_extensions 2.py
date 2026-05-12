"""Tests for the Phase 11 extensions.

Coverage:

- Critic agent: flags uncited claims, unsafe action recommendations,
  allergy-medication contradictions (penicillin + amoxicillin),
  patient-facing tone.
- Referral fax schema: confidence floor enforcement, model_dump
  round-trips.
- Lab trend widget: empty list returns placeholder, single point
  renders dot, multi-point renders polyline + table; SVG markers carry
  data-citation-id.
- Contextual rewriter: appends problem-derived synonyms; falls through
  to base rewriter when patient is empty; preserves the base rewriter
  expansions.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from sidecar.agents.w2.state import ClinicalClaim, ResponsePacket
from sidecar.critic.critic_agent import CRITIC_PROMPT_VERSION, review
from sidecar.rag.contextual_rewriter import (
    ContextualPatientFacts,
    ContextualRewriter,
)
from sidecar.schemas.w2.referral import REFERRAL_FIELD_CONFIDENCE_FLOOR, ReferralFaxExtraction
from sidecar.widgets.lab_trend import TrendPoint, render_trend


# ─── Critic ───────────────────────────────────────────────────────────


def _packet(claims: list[ClinicalClaim]) -> ResponsePacket:
    return ResponsePacket(summary="x", claims=claims)


def test_critic_flags_unsafe_action_without_guideline_cite() -> None:
    packet = _packet(
        [
            ClinicalClaim(
                text="Increase metformin to 1000 mg twice daily.",
                citations=["DocumentReference/doc-1"],
            )
        ]
    )
    report = review(packet, patient_allergies=[], patient_medications=[])
    rules = {f.rule for f in report.findings}
    assert "unsafe_action" in rules


def test_critic_passes_action_with_guideline_cite() -> None:
    packet = _packet(
        [
            ClinicalClaim(
                text="Increase metformin per ADA Standards of Care.",
                citations=["ADA-Standards-of-Care-2025-glycemic-targets"],
            )
        ]
    )
    report = review(packet, patient_allergies=[], patient_medications=[])
    rules = {f.rule for f in report.findings}
    assert "unsafe_action" not in rules


def test_critic_flags_penicillin_amoxicillin_contradiction() -> None:
    packet = _packet(
        [
            ClinicalClaim(
                text="Patient has a penicillin allergy and is currently on amoxicillin.",
                citations=["DocumentReference/intake-1"],
            )
        ]
    )
    report = review(
        packet,
        patient_allergies=["penicillin"],
        patient_medications=["amoxicillin"],
    )
    rules = {f.rule for f in report.findings}
    assert "contradiction" in rules


def test_critic_flags_patient_tone() -> None:
    packet = _packet(
        [
            ClinicalClaim(
                text="You should take metformin twice a day with meals.",
                citations=["ADA-Standards-of-Care-2025"],
            )
        ]
    )
    report = review(packet, patient_allergies=[], patient_medications=[])
    rules = {f.rule for f in report.findings}
    assert "patient_tone" in rules


def test_critic_clean_response_has_no_findings() -> None:
    packet = _packet(
        [
            ClinicalClaim(
                text="HbA1c is 6.8 percent, within target.",
                citations=["DocumentReference/doc-1", "ADA-Standards-of-Care-2025"],
            )
        ]
    )
    report = review(packet, patient_allergies=[], patient_medications=[])
    assert report.is_clean
    assert report.prompt_version == CRITIC_PROMPT_VERSION


# ─── Referral schema ──────────────────────────────────────────────────


def test_referral_extraction_round_trip() -> None:
    extraction = ReferralFaxExtraction(
        document_id="doc-r1",
        document_sha256="a" * 64,
        patient_id="Patient/87413",
        page_count=1,
        extracted_at=datetime.utcnow(),
        extracted_by_model="stub",
        prompt_version="referral.v1",
        referring_provider="Dr. Smith",
        reason_for_referral="Worsening renal function",
        requested_service="Nephrology consult",
        prior_authorization_indicated=False,
        confidence=0.9,
        source_quote="Worsening renal function; please evaluate",
    )
    payload = extraction.model_dump(mode="json")
    again = ReferralFaxExtraction.model_validate(payload, strict=False)
    assert again.referring_provider == "Dr. Smith"


def test_referral_extraction_below_floor_rejected() -> None:
    with pytest.raises(ValueError):
        ReferralFaxExtraction(
            document_id="doc-r2",
            document_sha256="a" * 64,
            patient_id="Patient/87413",
            page_count=1,
            extracted_at=datetime.utcnow(),
            extracted_by_model="stub",
            prompt_version="referral.v1",
            reason_for_referral="x",
            requested_service="y",
            confidence=REFERRAL_FIELD_CONFIDENCE_FLOOR - 0.05,
            source_quote="some quote",
        )


# ─── Lab trend widget ─────────────────────────────────────────────────


def test_render_trend_empty_returns_placeholder() -> None:
    out = render_trend(test_name="HbA1c", points=[])
    assert "trend-empty" in out
    assert "HbA1c" in out


def test_render_trend_single_point_renders_dot() -> None:
    out = render_trend(
        test_name="HbA1c",
        points=[
            TrendPoint(when=date(2026, 4, 15), value=6.8, unit="%", citation_id="cit-1"),
        ],
    )
    assert "trend-single" in out
    assert "6.8 %" in out
    assert "data-citation-id='cit-1'" in out


def test_render_trend_multi_point_renders_polyline_and_table() -> None:
    out = render_trend(
        test_name="HbA1c",
        points=[
            TrendPoint(when=date(2025, 11, 1), value=7.4, unit="%", citation_id="cit-old"),
            TrendPoint(when=date(2026, 2, 1), value=7.0, unit="%", citation_id="cit-mid"),
            TrendPoint(when=date(2026, 4, 15), value=6.8, unit="%", citation_id="cit-new"),
        ],
    )
    assert "<polyline" in out
    assert 'data-citation-id="cit-old"' in out
    assert "<tbody>" in out
    # Row count: 3 data points → 3 <tr> rows in tbody.
    assert out.count("<tr>") == 4  # 1 header + 3 data rows


# ─── Contextual rewriter ──────────────────────────────────────────────


async def test_contextual_rewriter_falls_through_without_patient() -> None:
    rewriter = ContextualRewriter()
    result = await rewriter.rewrite("HbA1c target")
    # Base rewriter expanded HbA1c; context added nothing.
    assert "glycated hemoglobin" in result.rewritten


async def test_contextual_rewriter_expands_with_problem_match() -> None:
    rewriter = ContextualRewriter(
        patient=ContextualPatientFacts(
            active_problems=["chronic kidney disease"]
        )
    )
    result = await rewriter.rewrite("What is her creatinine trend?")
    assert "estimated glomerular filtration rate" in result.rewritten


async def test_contextual_rewriter_no_op_when_problem_does_not_match_query() -> None:
    rewriter = ContextualRewriter(
        patient=ContextualPatientFacts(active_problems=["diabetes"])
    )
    result = await rewriter.rewrite("Should she get a flu shot?")
    # No problem-derived expansion because "control" / "a1c" not in query.
    assert "glycemic control" not in result.rewritten
