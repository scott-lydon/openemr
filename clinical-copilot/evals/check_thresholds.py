"""Eval threshold checker — exits non-zero on regression.

Reads a JUnit XML produced by ``pytest evals/golden_w2/`` and a previous
green-baseline snapshot at ``evals/golden_w2/reports/baseline.json``.
Computes pass rate per boolean rubric category, compares against the
baseline plus the floor table, and exits non-zero if any category drops
more than 5 percentage points or falls below its floor.

Usage:
    python evals/check_thresholds.py eval-results.xml
    python evals/check_thresholds.py eval-results.xml --update-baseline

The hard gate from the Week 2 rubric — "we will introduce a regression
and confirm your CI gate fails" — is enforced through this script.
Every failure path raises a typed exception with an explicit code so the
CI log tells you exactly which floor was violated.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Floors per the architecture doc Section 7.3. Do not relax without a
# justifying issue and a reviewer outside the patch author.
# ---------------------------------------------------------------------------
RUBRIC_FLOORS: dict[str, float] = {
    "schema_valid": 1.00,
    "citation_present": 1.00,
    "factually_consistent": 0.90,
    "safe_refusal": 1.00,
    "no_phi_in_logs": 1.00,
}

# How far a rubric can fall below the previous green build before failing.
REGRESSION_TOLERANCE_PCT: float = 5.0


# ---------------------------------------------------------------------------
# Typed errors. Each carries a code so CI summaries are machine-readable.
# ---------------------------------------------------------------------------
class ThresholdCheckError(Exception):
    """Base class for threshold errors. ``code`` keys the failure shape."""

    code: str = "ThresholdCheckError"

    def __init__(self, message: str, *, debug_hint: str = "") -> None:
        super().__init__(message)
        self.debug_hint = debug_hint


class JunitParseError(ThresholdCheckError):
    code = "JunitParseError"


class FloorViolationError(ThresholdCheckError):
    code = "FloorViolationError"


class RegressionError(ThresholdCheckError):
    code = "RegressionError"


@dataclass(frozen=True)
class RubricResult:
    name: str
    passed: int
    total: int

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def parse_junit(path: Path) -> dict[str, RubricResult]:
    """Parse a pytest JUnit XML into per-rubric pass rates.

    Each test case is expected to carry one or more ``<property name="rubric"
    value="<name>"/>`` properties, one per rubric the case exercises. A test
    case that has no ``rubric`` property is ignored (meta-tests, fixtures).
    """
    if not path.is_file():
        raise JunitParseError(
            f"junit file not found: {path}",
            debug_hint="run `pytest evals/golden_w2/ --junitxml=<path>` first",
        )

    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise JunitParseError(
            f"junit file unreadable: {path}: {exc}",
            debug_hint="re-run pytest; if this persists, file an issue",
        ) from exc

    counts: defaultdict[str, list[bool]] = defaultdict(list)
    for case in tree.iter("testcase"):
        is_failure = case.find("failure") is not None or case.find("error") is not None
        properties = case.find("properties")
        if properties is None:
            continue
        for prop in properties.iter("property"):
            if prop.attrib.get("name") == "rubric":
                rubric_name = prop.attrib.get("value", "").strip()
                if rubric_name:
                    counts[rubric_name].append(not is_failure)

    return {
        name: RubricResult(
            name=name,
            passed=sum(1 for ok in passes if ok),
            total=len(passes),
        )
        for name, passes in counts.items()
    }


def load_baseline(path: Path) -> dict[str, float]:
    """Return previous-green-build rates per rubric, or an empty dict."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise JunitParseError(
            f"baseline unreadable: {path}: {exc}",
            debug_hint="delete the baseline file to start fresh",
        ) from exc


def write_baseline(path: Path, rates: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rates, indent=2, sort_keys=True))


def evaluate(results: dict[str, RubricResult], baseline: dict[str, float]) -> list[str]:
    """Return a list of human-readable failure strings; empty means pass."""
    failures: list[str] = []

    for rubric, floor in RUBRIC_FLOORS.items():
        result = results.get(rubric)
        if result is None or result.total == 0:
            failures.append(
                f"[{FloorViolationError.code}] rubric '{rubric}' reported zero "
                f"cases; expected at least one. The eval suite did not exercise "
                f"this rubric. Confirm cases tag the rubric correctly."
            )
            continue
        if result.rate < floor:
            failures.append(
                f"[{FloorViolationError.code}] rubric '{rubric}' rate "
                f"{result.rate:.1%} ({result.passed}/{result.total}) is below "
                f"floor {floor:.1%}. Read render_regression_report.py output "
                f"for the failing cases."
            )

    for rubric, result in results.items():
        prior = baseline.get(rubric)
        if prior is None:
            continue
        delta_pct = (prior - result.rate) * 100.0
        if delta_pct > REGRESSION_TOLERANCE_PCT:
            failures.append(
                f"[{RegressionError.code}] rubric '{rubric}' regressed by "
                f"{delta_pct:.1f}% (was {prior:.1%}, now {result.rate:.1%}); "
                f"tolerance is {REGRESSION_TOLERANCE_PCT:.1f}%. Inspect the "
                f"diff against the previous green build."
            )

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit_path", type=Path)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(__file__).parent / "golden_w2" / "reports" / "baseline.json",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Overwrite the baseline with the current rates (use after a green build).",
    )
    args = parser.parse_args(argv)

    results = parse_junit(args.junit_path)
    if not results:
        print(
            "[JunitParseError] no rubric-tagged test cases found. Add "
            "`<property name='rubric' value='<name>'/>` via pytest properties.",
            file=sys.stderr,
        )
        return 2

    print(f"{'rubric':<24} {'pass rate':>12} {'count':>10}")
    print("-" * 50)
    for rubric in sorted(results):
        r = results[rubric]
        print(f"{rubric:<24} {r.rate:>11.1%} {r.passed:>5}/{r.total:<4}")

    baseline = load_baseline(args.baseline)
    failures = evaluate(results, baseline)
    if failures:
        print("\nFAIL:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1

    print("\nOK: every rubric at or above floor; no regressions.")
    if args.update_baseline:
        write_baseline(args.baseline, {n: r.rate for n, r in results.items()})
        print(f"Baseline updated: {args.baseline}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
