"""Tests for the data-gap rule table.

The verifier's data-gap reporting was previously a hand-rolled chain
of ``if gout_problem...`` / ``if dm_problem...`` blocks. It is now a
loop over :data:`DATA_GAP_RULES`. These tests exercise the rule
table directly so a future contributor adding a new rule has a clear
failure path when the rule shape regresses, and a working template
to copy from.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sidecar.snapshot.models import (
    Demographics,
    LabObservation,
    PatientSnapshot,
    Problem,
    Provenance,
)
from sidecar.verifier.data_gap_rules import DATA_GAP_RULES
from sidecar.verifier.verifier import Verifier


def _problem(label: str) -> Problem:
    return Problem(
        id=f"Condition/test-{label}",
        label=label,
        provenance=Provenance(table="lists", row_id=1),
    )


def _lab(label: str) -> LabObservation:
    observed = datetime.now(tz=timezone.utc)
    return LabObservation(
        id="Observation/test",
        label=label,
        loinc="0",  # Test fixture; the verifier ignores LOINC.
        value=1.0,
        unit="mg/dL",
        observed_at=observed,
        provenance=Provenance(
            table="procedure_result",
            row_id=1,
            observed_at=observed,
        ),
    )


def _snapshot(problems: list[Problem], labs: list[LabObservation]) -> PatientSnapshot:
    return PatientSnapshot(
        patient_id="Patient/test",
        snapshot_version=datetime.now(tz=timezone.utc),
        demographics=Demographics(),
        active_problems=problems,
        recent_labs=labs,
    )


def test_rule_table_has_at_least_one_entry() -> None:
    """The table is the verifier's only source of gaps now; an empty
    table would silently disable the feature."""
    assert len(DATA_GAP_RULES) >= 1


def test_gout_rule_fires_when_uric_acid_missing() -> None:
    snap = _snapshot(problems=[_problem("Gout, unspecified")], labs=[])
    gaps = Verifier._data_gaps(snap)
    assert any("uric acid" in g and "Gout, unspecified" in g for g in gaps)


def test_gout_rule_silenced_when_uric_acid_present() -> None:
    snap = _snapshot(
        problems=[_problem("Gout, unspecified")],
        labs=[_lab("Uric acid, serum")],
    )
    gaps = Verifier._data_gaps(snap)
    assert not any("uric acid" in g for g in gaps)


def test_diabetes_rule_fires_when_a1c_missing() -> None:
    snap = _snapshot(problems=[_problem("Type 2 diabetes mellitus")], labs=[])
    gaps = Verifier._data_gaps(snap)
    assert any("HbA1c" in g and "Type 2 diabetes mellitus" in g for g in gaps)


def test_diabetes_rule_silenced_when_a1c_present() -> None:
    snap = _snapshot(
        problems=[_problem("Type 2 diabetes mellitus")],
        labs=[_lab("Hemoglobin A1c")],
    )
    gaps = Verifier._data_gaps(snap)
    assert not any("HbA1c" in g for g in gaps)


def test_unknown_problem_yields_no_gaps() -> None:
    """A patient whose problem list does not match any rule should
    receive zero gaps. This is the contract that lets us add new
    rules without surprising existing patients."""
    snap = _snapshot(problems=[_problem("Bronchitis")], labs=[])
    assert Verifier._data_gaps(snap) == []


def test_gaps_capped_at_two() -> None:
    """Both rules fire — we still surface at most two so the UI does
    not become an alert wall."""
    snap = _snapshot(
        problems=[
            _problem("Gout, unspecified"),
            _problem("Type 2 diabetes mellitus"),
        ],
        labs=[],
    )
    gaps = Verifier._data_gaps(snap)
    assert len(gaps) <= 2


def test_message_template_uses_problem_label_verbatim() -> None:
    """Regression: the gap message used to interpolate a free-form
    string. The template now formats with ``{problem}`` and we want
    to keep the exact label so the doctor can grep for it."""
    snap = _snapshot(
        problems=[_problem("Acute gout flare, right MTP joint")],
        labs=[],
    )
    gaps = Verifier._data_gaps(snap)
    assert any(
        "Acute gout flare, right MTP joint" in g for g in gaps
    )
