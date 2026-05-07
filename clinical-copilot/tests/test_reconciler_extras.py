"""Regression tests for the medication-label and Patient-resource
helpers added to reconciler.py.

These exist as a separate module from ``test_reconciler.py`` to keep
the new behaviour easy to find and to make CI failures point
directly at the helper under test rather than the larger reconcile()
integration suite.
"""

from __future__ import annotations

import pytest

from sidecar.snapshot.reconciler import (
    _demographics_from_patient_resource,
    _human_name,
    _medication_label,
)


# ─── _medication_label fallbacks ──────────────────────────────────────


@pytest.mark.parametrize(
    "resource, expected",
    [
        # 1. Standard FHIR: medicationCodeableConcept.text wins.
        (
            {"medicationCodeableConcept": {"text": "Metformin 500 mg"}},
            "Metformin 500 mg",
        ),
        # 2. coding[].display when text is absent.
        (
            {
                "medicationCodeableConcept": {
                    "coding": [
                        {
                            "system": "http://snomed.info/sct",
                            "code": "1",
                            "display": "Lisinopril",
                        }
                    ]
                }
            },
            "Lisinopril",
        ),
        # 3. medicationReference.display (FHIR-reference style).
        (
            {
                "medicationReference": {
                    "reference": "Medication/abc",
                    "display": "Atorvastatin 40 mg",
                }
            },
            "Atorvastatin 40 mg",
        ),
        # 4. contained Medication.code.text.
        (
            {
                "contained": [
                    {
                        "resourceType": "Medication",
                        "code": {"text": "Aspirin 81 mg"},
                    }
                ]
            },
            "Aspirin 81 mg",
        ),
        # 5. Last-resort RxNorm code rendering when no display is anywhere.
        (
            {
                "medicationCodeableConcept": {
                    "coding": [
                        {
                            "system": (
                                "http://www.nlm.nih.gov/research/umls/rxnorm"
                            ),
                            "code": "6809",
                        }
                    ]
                }
            },
            "RxNorm:6809",
        ),
        # 6. Empty resource returns empty string (caller decides what to do).
        ({}, ""),
        # 7. text takes priority over coding[].display when both exist.
        (
            {
                "medicationCodeableConcept": {
                    "text": "Real Name",
                    "coding": [{"display": "Wrong"}],
                }
            },
            "Real Name",
        ),
        # 8. Whitespace-only text falls through to coding[].display.
        (
            {
                "medicationCodeableConcept": {
                    "text": "   ",
                    "coding": [{"display": "OK"}],
                }
            },
            "OK",
        ),
    ],
)
def test_medication_label_fallbacks(
    resource: dict, expected: str
) -> None:
    """Each FHIR shape we have observed in OpenEMR or in the spec
    should yield a usable label, or empty string only when the
    resource carries no human-readable identification at all.

    @codeCoverageIgnore Data providers run before coverage instrumentation.
    """
    assert _medication_label(resource) == expected


# ─── _human_name preference order ─────────────────────────────────────


def test_human_name_prefers_official() -> None:
    name = _human_name(
        [
            {"use": "nickname", "given": ["Barb"]},
            {"use": "official", "family": "Boston", "given": ["Barbara"]},
        ]
    )
    assert name == "Barbara Boston"


def test_human_name_falls_back_to_text() -> None:
    name = _human_name([{"text": "Boston, Barbara"}])
    assert name == "Boston, Barbara"


def test_human_name_handles_only_family() -> None:
    name = _human_name([{"family": "Boston"}])
    assert name == "Boston"


def test_human_name_returns_none_for_empty() -> None:
    assert _human_name([]) is None


# ─── _demographics_from_patient_resource ──────────────────────────────


def test_demographics_full_patient_resource() -> None:
    barbara = {
        "resourceType": "Patient",
        "id": "87413000-0000-4000-8000-000000000000",
        "name": [
            {
                "use": "official",
                "family": "Boston",
                "given": ["Barbara"],
            }
        ],
        "gender": "female",
        "birthDate": "1955-04-12",
    }
    d = _demographics_from_patient_resource(barbara)
    assert d.name == "Barbara Boston"
    assert d.sex_at_birth == "female"
    assert d.dob is not None and d.dob.isoformat() == "1955-04-12"
    # Age check is intentionally a range so the test does not break on
    # birthday wraparound while leaving a clear failure if the math
    # regresses.
    assert d.age is not None and 70 <= d.age <= 72


def test_demographics_missing_resource_returns_empty() -> None:
    d = _demographics_from_patient_resource(None)
    assert d.name is None
    assert d.age is None
    assert d.sex_at_birth is None
    assert d.dob is None


def test_demographics_invalid_birthdate_does_not_crash() -> None:
    d = _demographics_from_patient_resource({"birthDate": "not-a-date"})
    assert d.dob is None
    assert d.age is None


def test_demographics_partial_birthdate_iso_prefix() -> None:
    """OpenEMR sometimes emits ``birthDate`` as a full ISO datetime
    rather than the ``YYYY-MM-DD`` string FHIR specifies. The first
    10 characters should still parse cleanly."""
    d = _demographics_from_patient_resource(
        {"birthDate": "1955-04-12T00:00:00+00:00"}
    )
    assert d.dob is not None and d.dob.isoformat() == "1955-04-12"
