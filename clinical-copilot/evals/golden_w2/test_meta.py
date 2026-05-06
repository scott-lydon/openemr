"""Meta-tests for the golden eval suite.

These tests do NOT run the agent — they validate the case dataset
itself. The Phase 8 contract is "every case has a documented failure
mode and rationale, every fixture path resolves, the dataset is at
least 50 cases." A regression in any of these properties is a content
bug, not an agent bug, and the meta-test catches it before CI runs the
expensive agent eval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.golden_w2.loader import load_cases


GOLDEN_ROOT = Path(__file__).resolve().parent
MIN_CASE_COUNT = 50


def test_dataset_loads_without_error() -> None:
    """Every JSONL line parses against the case schema."""
    cases = load_cases(GOLDEN_ROOT)
    assert len(cases) >= 1, "no cases were loaded; check JSONL files"


def test_every_case_has_failure_mode_targeted_and_rationale() -> None:
    cases = load_cases(GOLDEN_ROOT)
    for case in cases:
        assert case.failure_mode_targeted.strip(), (
            f"case {case.id} has empty failure_mode_targeted"
        )
        assert case.rationale.strip(), f"case {case.id} has empty rationale"


def test_every_fixture_path_resolves_or_marks_pending() -> None:
    """Every ``documents`` path either exists or is tagged 'fixture_pending'.

    The eval harness skips cases whose fixtures are pending, so a stub
    case that names a not-yet-built fixture is not a hard failure
    while the fixtures are being authored.
    """
    cases = load_cases(GOLDEN_ROOT)
    for case in cases:
        if "fixture_pending" in case.tags:
            continue
        for relative in case.documents:
            absolute = (GOLDEN_ROOT / relative).resolve()
            assert absolute.exists(), (
                f"case {case.id} references missing fixture {relative!r} "
                f"(resolved to {absolute}); add the fixture or tag the case "
                "with 'fixture_pending'."
            )


def test_case_ids_are_unique_and_well_formed() -> None:
    cases = load_cases(GOLDEN_ROOT)
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids)), (
        f"duplicate case ids: {[i for i in ids if ids.count(i) > 1]}"
    )
    for case in cases:
        assert case.id.startswith("w2-"), (
            f"case id {case.id!r} should start with 'w2-' for repo grep"
        )
        assert case.id[3:].replace("-", "").isalnum(), (
            f"case id {case.id!r} contains characters outside [a-z0-9-]"
        )


def test_every_case_has_max_latency_budget() -> None:
    """Latency budgets are required; un-budgeted cases hide latency
    regressions because the threshold checker has nothing to compare to."""
    cases = load_cases(GOLDEN_ROOT)
    missing = [case.id for case in cases if case.expected.max_latency_ms_p95 is None]
    assert not missing, (
        f"cases missing max_latency_ms_p95: {missing}; add a budget to each "
        "or document the omission."
    )


def test_dataset_has_at_least_fifty_cases() -> None:
    """The rubric's minimum 50-case target.

    The dataset overshoots by a small margin so a single flaky case
    being temporarily disabled does not push us under the floor.
    """
    cases = load_cases(GOLDEN_ROOT)
    assert len(cases) >= MIN_CASE_COUNT, (
        f"only {len(cases)} cases; need at least {MIN_CASE_COUNT}"
    )
