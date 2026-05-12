"""Strict-schema invariants for Week 2 extraction models.

Each test names the failure mode it protects against and the rubric it
maps to. Tests carry a pytest property ``rubric`` so the threshold
checker (``evals/check_thresholds.py``) attributes failures to the
``schema_valid`` rubric.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from sidecar.schemas.w2 import (
    AbnormalFlag,
    AllergyField,
    BoundingBox,
    Citation,
    CitationSourceType,
    DemographicsBlock,
    ExtractionWarning,
    ExtractionWarningCode,
    IntakeFormExtraction,
    LabPdfExtraction,
    LabResultField,
    MedicationField,
    SexCode,
)
from sidecar.schemas.w2.lab import LAB_FIELD_CONFIDENCE_FLOOR


def _record_rubric(record_property, name: str) -> None:
    """Pytest plumbing: tag the JUnit XML record with the rubric name."""
    record_property("rubric", name)


# ---------------------------------------------------------------------------
# BoundingBox invariants
# ---------------------------------------------------------------------------


def test_bbox_accepts_normalized_coords(record_property) -> None:
    _record_rubric(record_property, "schema_valid")
    bb = BoundingBox(page=0, x0=0.10, y0=0.20, x1=0.50, y1=0.30)
    assert bb.page == 0


def test_bbox_rejects_out_of_range(record_property) -> None:
    _record_rubric(record_property, "schema_valid")
    with pytest.raises(ValidationError):
        BoundingBox(page=0, x0=-0.1, y0=0.0, x1=0.5, y1=0.5)
    with pytest.raises(ValidationError):
        BoundingBox(page=0, x0=0.0, y0=0.0, x1=1.5, y1=0.5)


def test_bbox_rejects_zero_width(record_property) -> None:
    """A zero-width bbox draws an invisible rectangle; reject at parse time."""
    _record_rubric(record_property, "schema_valid")
    with pytest.raises(ValidationError) as exc_info:
        BoundingBox(page=2, x0=0.4, y0=0.1, x1=0.4, y1=0.5)
    # Error message must name the offending field and the page.
    assert "page 2" in str(exc_info.value)
    assert "x1" in str(exc_info.value)


def test_bbox_rejects_zero_height(record_property) -> None:
    _record_rubric(record_property, "schema_valid")
    with pytest.raises(ValidationError) as exc_info:
        BoundingBox(page=0, x0=0.1, y0=0.5, x1=0.6, y1=0.5)
    assert "y1" in str(exc_info.value)


def test_bbox_rejects_inverted_coords(record_property) -> None:
    _record_rubric(record_property, "schema_valid")
    with pytest.raises(ValidationError):
        BoundingBox(page=0, x0=0.6, y0=0.1, x1=0.2, y1=0.5)


# ---------------------------------------------------------------------------
# LabResultField invariants
# ---------------------------------------------------------------------------


def _ok_lab_field(**overrides) -> LabResultField:
    base = dict(
        test_name="HbA1c",
        value="6.8",
        value_numeric=6.8,
        unit="%",
        reference_range_low=4.0,
        reference_range_high=5.6,
        abnormal_flag=AbnormalFlag.HIGH,
        collection_date=date(2026, 4, 15),
        confidence=0.92,
        bbox=BoundingBox(page=0, x0=0.1, y0=0.2, x1=0.4, y1=0.25),
        source_quote="HbA1c    6.8 %",
    )
    base.update(overrides)
    return LabResultField(**base)


def test_lab_field_accepts_well_formed(record_property) -> None:
    _record_rubric(record_property, "schema_valid")
    field = _ok_lab_field()
    assert field.test_name == "HbA1c"


def test_lab_field_rejects_unknown_field(record_property) -> None:
    """``extra='forbid'``: hallucinated fields become ValidationError."""
    _record_rubric(record_property, "schema_valid")
    with pytest.raises(ValidationError):
        LabResultField(  # type: ignore[call-arg]
            test_name="HbA1c",
            value="6.8",
            abnormal_flag=AbnormalFlag.HIGH,
            confidence=0.92,
            source_quote="HbA1c 6.8 %",
            invented_field="this should not be allowed",
        )


def test_lab_field_drops_below_confidence_floor(record_property) -> None:
    """A below-floor candidate must never reach the schema layer."""
    _record_rubric(record_property, "schema_valid")
    with pytest.raises(ValidationError) as exc_info:
        _ok_lab_field(confidence=0.4)
    assert "below the floor" in str(exc_info.value)
    # Floor value must be in the message so debugging is one-grep away.
    assert f"{LAB_FIELD_CONFIDENCE_FLOOR:.2f}" in str(exc_info.value)


def test_lab_field_requires_anchor(record_property) -> None:
    """A field with no bbox AND no source_quote has no citation anchor."""
    _record_rubric(record_property, "schema_valid")
    with pytest.raises(ValidationError) as exc_info:
        _ok_lab_field(bbox=None, source_quote="   ")
    assert "anchor" in str(exc_info.value)


def test_lab_field_accepts_quote_only_when_bbox_absent(record_property) -> None:
    """Scanned/handwritten labs may have a quote but no bbox; allow that."""
    _record_rubric(record_property, "schema_valid")
    field = _ok_lab_field(bbox=None, source_quote="HbA1c     6.8 %")
    assert field.bbox is None
    assert field.source_quote.strip() != ""


# ---------------------------------------------------------------------------
# LabPdfExtraction shape
# ---------------------------------------------------------------------------


def test_lab_extraction_round_trips(record_property) -> None:
    _record_rubric(record_property, "schema_valid")
    e = LabPdfExtraction(
        document_id="DocumentReference/abcd",
        document_sha256="a" * 64,
        patient_id="Patient/87413",
        page_count=2,
        extracted_at=datetime.now(UTC),
        extracted_by_model="gpt-4o-2024-11-20",
        prompt_version="v1.0.0",
        results=[_ok_lab_field()],
        extraction_warnings=[
            ExtractionWarning(code=ExtractionWarningCode.LOW_CONFIDENCE, message="noisy scan"),
        ],
    )
    same = LabPdfExtraction.model_validate_json(e.model_dump_json())
    assert same.results[0].test_name == e.results[0].test_name


def test_lab_extraction_rejects_bad_sha256(record_property) -> None:
    _record_rubric(record_property, "schema_valid")
    with pytest.raises(ValidationError):
        LabPdfExtraction(
            document_id="DocumentReference/abcd",
            document_sha256="too-short",
            patient_id="Patient/87413",
            page_count=1,
            extracted_at=datetime.now(UTC),
            extracted_by_model="gpt-4o",
            prompt_version="v1.0.0",
            results=[],
        )


# ---------------------------------------------------------------------------
# IntakeFormExtraction shape
# ---------------------------------------------------------------------------


def test_intake_form_well_formed(record_property) -> None:
    _record_rubric(record_property, "schema_valid")
    bbox = BoundingBox(page=0, x0=0.1, y0=0.2, x1=0.4, y1=0.25)
    e = IntakeFormExtraction(
        document_id="DocumentReference/intk-1",
        document_sha256="b" * 64,
        patient_id="Patient/87413",
        page_count=1,
        extracted_at=datetime.now(UTC),
        extracted_by_model="gpt-4o-2024-11-20",
        prompt_version="v1.0.0",
        demographics=DemographicsBlock(
            full_name="Jane Doe",
            dob=date(1972, 5, 4),
            sex=SexCode.FEMALE,
            confidence=0.99,
            bbox=bbox,
            source_quote="Name: Jane Doe  DOB: 5/4/1972  Sex: F",
        ),
        chief_concern="Recurrent left knee pain over the last two months.",
        current_medications=[
            MedicationField(
                name="Metformin",
                dose="500mg",
                frequency="twice daily",
                confidence=0.91,
                source_quote="Metformin 500mg BID",
                bbox=bbox,
            ),
        ],
        allergies=[
            AllergyField(
                substance="Penicillin",
                reaction="hives",
                severity="moderate",
                confidence=0.88,
                source_quote="Penicillin -> hives (moderate)",
                bbox=bbox,
            ),
        ],
    )
    assert e.allergies[0].substance == "Penicillin"


# ---------------------------------------------------------------------------
# Citation contract
# ---------------------------------------------------------------------------


def test_citation_document_requires_anchor(record_property) -> None:
    _record_rubric(record_property, "citation_present")
    with pytest.raises(ValidationError) as exc_info:
        Citation(
            source_type=CitationSourceType.DOCUMENT_REFERENCE,
            source_id="DocumentReference/abcd",
            page_or_section=1,
            field_or_chunk_id="results[0]",
            quote_or_value="   ",
            bbox=None,
        )
    assert "anchor" in str(exc_info.value)


def test_citation_guideline_does_not_need_bbox(record_property) -> None:
    _record_rubric(record_property, "citation_present")
    c = Citation(
        source_type=CitationSourceType.GUIDELINE,
        source_id="ADA-Standards-of-Care-2025",
        page_or_section="Glycemic Targets in Adults",
        field_or_chunk_id="ada-2025-glycemic-001",
        quote_or_value="A reasonable A1C goal for many nonpregnant adults is <7%.",
    )
    assert c.bbox is None
