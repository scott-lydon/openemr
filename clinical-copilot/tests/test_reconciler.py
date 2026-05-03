"""Unit tests for the deterministic reconciliation pass.

These run on a hand-built FHIR Bundle dict to keep tests fast and offline.
"""

from __future__ import annotations

from sidecar.snapshot.reconciler import reconcile


def test_reconciler_collapses_duplicate_medications_and_flags_disagreement() -> None:
    """Same RxNorm in two sources with different ``active`` flag → flag raised."""
    bundles = {
        "active_problems": None,
        "encounter_diagnoses": None,
        "medications": {
            "entry": [
                {
                    "resource": {
                        "id": "9001",
                        "status": "active",
                        "medicationCodeableConcept": {
                            "text": "Metformin 500 mg",
                            "coding": [
                                {"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                 "code": "860975"}
                            ],
                        },
                        "authoredOn": "2014-03-15",
                    },
                },
                {
                    "resource": {
                        "id": "9001-dup",
                        "status": "completed",
                        "medicationCodeableConcept": {
                            "text": "Metformin 500 mg",
                            "coding": [
                                {"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                 "code": "860975"}
                            ],
                        },
                        "authoredOn": "2018-01-01",
                    },
                },
            ]
        },
        "allergies": None, "vitals": None, "labs": None,
    }
    snap = reconcile(patient_uuid="Patient/1", fhir_bundles=bundles)
    assert len(snap.medications) == 1
    assert snap.medications[0].sources_in_agreement is False
    flag_codes = {f.code for f in snap.quality_flags}
    assert "med_disagreement" in flag_codes


def test_reconciler_maps_free_text_to_icd10() -> None:
    bundles = {
        "active_problems": {
            "entry": [
                {
                    "resource": {
                        "id": "p1",
                        "code": {"text": "Gout", "coding": []},
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "verificationStatus": {"coding": [{"code": "confirmed"}]},
                        "onsetDateTime": "2019-06-04",
                    }
                }
            ]
        },
        "encounter_diagnoses": None, "medications": None,
        "allergies": None, "vitals": None, "labs": None,
    }
    snap = reconcile(patient_uuid="Patient/1", fhir_bundles=bundles)
    assert len(snap.active_problems) == 1
    assert snap.active_problems[0].icd10 == "M10.9"


def test_reconciler_reads_conditions_from_unified_bundle() -> None:
    """Sidecar pulls a single ``Condition?patient={pid}`` (no filters)
    because OpenEMR's filtered Condition query crashes; the reconciler
    splits + filters in code instead."""
    bundles = {
        "conditions": {
            "entry": [
                {"resource": {
                    "id": "p1", "code": {"text": "Gout"},
                    "clinicalStatus": {"coding": [{"code": "active"}]},
                    "verificationStatus": {"coding": [{"code": "confirmed"}]},
                }},
                {"resource": {
                    "id": "p2", "code": {"text": "Hypertension"},
                    "clinicalStatus": {"coding": [{"code": "active"}]},
                    "verificationStatus": {"coding": [{"code": "confirmed"}]},
                }},
            ],
        },
        "medications": None, "allergies": None,
        "vitals": None, "labs": None,
    }
    snap = reconcile(patient_uuid="Patient/87413", fhir_bundles=bundles)
    titles = sorted(p.label for p in snap.active_problems)
    assert titles == ["Gout", "Hypertension"]


def test_reconciler_filters_out_resolved_conditions() -> None:
    """A resolved condition should not be paired against new symptoms."""
    bundles = {
        "conditions": {
            "entry": [
                {"resource": {
                    "id": "p1", "code": {"text": "Gout (active)"},
                    "clinicalStatus": {"coding": [{"code": "active"}]},
                }},
                {"resource": {
                    "id": "p2", "code": {"text": "Pneumonia (resolved)"},
                    "clinicalStatus": {"coding": [{"code": "resolved"}]},
                }},
                {"resource": {
                    "id": "p3", "code": {"text": "Sinusitis (inactive)"},
                    "clinicalStatus": {"coding": [{"code": "inactive"}]},
                }},
            ],
        },
        "medications": None, "allergies": None,
        "vitals": None, "labs": None,
    }
    snap = reconcile(patient_uuid="Patient/87413", fhir_bundles=bundles)
    labels = [p.label for p in snap.active_problems]
    assert labels == ["Gout (active)"]


def test_reconciler_treats_missing_clinical_status_as_active() -> None:
    """Old OpenEMR rows lack clinicalStatus; do not silently drop them."""
    bundles = {
        "conditions": {
            "entry": [{"resource": {"id": "p1", "code": {"text": "Old gout"}}}],
        },
        "medications": None, "allergies": None,
        "vitals": None, "labs": None,
    }
    snap = reconcile(patient_uuid="Patient/87413", fhir_bundles=bundles)
    assert [p.label for p in snap.active_problems] == ["Old gout"]


def test_reconciler_extracts_presenting_from_latest_encounter_reasonCode() -> None:
    """Encounter.reasonCode is OpenEMR's home for the chief complaint;
    the reconciler must pick the most recent encounter, split on `,` /
    `;`, and surface each symptom individually."""
    bundles = {
        "active_problems": None, "encounter_diagnoses": None,
        "medications": None, "allergies": None,
        "vitals": None, "labs": None,
        "encounters": {
            "entry": [
                {"resource": {
                    "resourceType": "Encounter",
                    "period": {"start": "2024-01-01"},
                    "reasonCode": [{"text": "old visit"}],
                }},
                {"resource": {
                    "resourceType": "Encounter",
                    "period": {"start": "2026-04-15T09:12:00Z"},
                    "reasonCode": [{
                        "text": "right toe pain, swollen toe, body aches; 3 days",
                    }],
                }},
            ],
        },
    }
    snap = reconcile(patient_uuid="Patient/87413", fhir_bundles=bundles)
    syms = [s.lower() for s in snap.presenting.symptoms]
    assert "right toe pain" in syms
    assert "swollen toe" in syms
    assert "body aches" in syms
    assert "old visit" not in syms  # latest-only, not concatenated
    assert snap.presenting.source == "Encounter.reasonCode"


def test_reconciler_falls_back_to_coding_display_when_text_missing() -> None:
    bundles = {
        "active_problems": None, "encounter_diagnoses": None,
        "medications": None, "allergies": None,
        "vitals": None, "labs": None,
        "encounters": {
            "entry": [{"resource": {
                "resourceType": "Encounter",
                "period": {"start": "2026-05-01"},
                "reasonCode": [{"coding": [{"display": "swollen toe"}]}],
            }}],
        },
    }
    snap = reconcile(patient_uuid="Patient/87413", fhir_bundles=bundles)
    assert snap.presenting.symptoms == ["swollen toe"]


def test_reconciler_caller_supplied_presenting_wins_over_encounter() -> None:
    """A BFF that has access to a pre-visit form should be able to
    pass presenting in directly without having it overwritten by the
    Encounter scrape."""
    from sidecar.snapshot.models import Presenting

    bundles = {
        "active_problems": None, "encounter_diagnoses": None,
        "medications": None, "allergies": None,
        "vitals": None, "labs": None,
        "encounters": {
            "entry": [{"resource": {
                "resourceType": "Encounter",
                "period": {"start": "2026-05-01"},
                "reasonCode": [{"text": "wrong text"}],
            }}],
        },
    }
    snap = reconcile(
        patient_uuid="Patient/87413",
        fhir_bundles=bundles,
        presenting=Presenting(symptoms=["pre-visit symptom"], source="patient portal"),
    )
    assert snap.presenting.symptoms == ["pre-visit symptom"]
    assert snap.presenting.source == "patient portal"


def test_reconciler_attaches_provenance_to_every_problem() -> None:
    bundles = {
        "active_problems": {
            "entry": [
                {
                    "resource": {
                        "id": "p1",
                        "code": {"text": "Gout", "coding": []},
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "verificationStatus": {"coding": [{"code": "confirmed"}]},
                    }
                }
            ]
        },
        "encounter_diagnoses": None, "medications": None,
        "allergies": None, "vitals": None, "labs": None,
    }
    snap = reconcile(patient_uuid="Patient/1", fhir_bundles=bundles)
    p = snap.active_problems[0]
    assert p.provenance.table == "lists"
    assert p.provenance.row_id == "p1"
    assert p.provenance.fhir_resource == "Condition/p1"
