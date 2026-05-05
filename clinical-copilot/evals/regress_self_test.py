#!/usr/bin/env python3
"""Hard gate: prove the eval gate fires on a deliberate regression.

The Week 2 rubric's hardest gate is "we will introduce a regression
and confirm your CI gate fails." This script:

1. Records the current state of a small, eval-critical module
   (``sidecar/agents/w2/lab_extractor.py``).
2. Patches that module with a deliberate one-line break (sets the
   confidence floor to 0.0, which lets every candidate field through).
3. Runs the unit-test suite. Expects it to fail.
4. Restores the original file.
5. Exits 0 only when both: (a) the patched run failed, and (b) the
   restored file exactly matches the original.

If the patched run passes, the eval gate is broken — the rubric's
hardest test would not catch a real regression. The script exits
non-zero in that case, with a message naming what went wrong.

Run with::

    [Local Mac terminal]
    cd /Users/scottlydon/Desktop/Clutter/iOS/openemr/clinical-copilot
    python evals/regress_self_test.py

Output ends with ``SELF TEST PASSED`` when the eval gate works.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final


REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
TARGET_FILE: Final[Path] = (
    REPO_ROOT / "sidecar" / "agents" / "w2" / "lab_extractor.py"
)
TEST_TARGET: Final[str] = "tests/sidecar/w2/test_extractor.py"


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_pytest() -> int:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_TARGET, "-q"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return completed.returncode


def main() -> int:
    if not TARGET_FILE.exists():
        sys.stderr.write(f"target file not found: {TARGET_FILE}\n")
        return 2

    original = TARGET_FILE.read_text()
    original_sha = _file_sha(TARGET_FILE)

    backup = TARGET_FILE.with_suffix(".self_test_backup")
    shutil.copy2(TARGET_FILE, backup)

    try:
        # Deliberate regression: set the confidence floor to 0.0 so
        # below-floor candidates that the test_extractor's "drops below
        # floor" test expects to be dropped now sneak through.
        if "LAB_FIELD_CONFIDENCE_FLOOR = 0.0" in original:
            sys.stderr.write(
                "Cannot apply the deliberate regression: floor is already "
                "0.0 in the source. The test expectation is wrong.\n"
            )
            return 2
        # The regression is not in lab_extractor itself — the floor is
        # imported from the schema. Patch the inlined check instead by
        # short-circuiting `_parse_candidate` to always return the
        # candidate without filtering. This forces the
        # `test_lab_extractor_drops_below_floor_with_warning` case to
        # fail.
        marker = "if isinstance(confidence, (int, float)) and confidence < LAB_FIELD_CONFIDENCE_FLOOR:"
        if marker not in original:
            sys.stderr.write(
                "Could not locate the floor-check marker; the lab "
                "extractor structure changed and this self-test needs "
                "an update.\n"
            )
            return 2
        patched = original.replace(
            marker,
            "if False and " + marker,  # disable the check
        )
        TARGET_FILE.write_text(patched)

        sys.stdout.write(
            "Applied deliberate regression. Running tests; expecting failure...\n"
        )
        rc = _run_pytest()
        if rc == 0:
            sys.stderr.write(
                "SELF TEST FAILED: tests passed despite the deliberate "
                "regression. The eval gate is BROKEN — fix the test "
                "coverage before submitting.\n"
            )
            return 1

        sys.stdout.write(
            "Tests failed as expected after the regression was applied.\n"
        )
    finally:
        # Always restore the original file, even on exceptions.
        TARGET_FILE.write_text(original)
        try:
            backup.unlink(missing_ok=True)
        except (PermissionError, OSError) as exc:
            # The backup file is harmless if it survives; log and
            # continue. Sandbox file systems sometimes refuse unlink
            # even when the owner attempts it; the restore above is the
            # contract that matters.
            sys.stderr.write(
                f"[regress_self_test] cleanup of {backup} failed: "
                f"{type(exc).__name__}: {exc}; ignoring (backup is "
                "harmless).\n"
            )

    if _file_sha(TARGET_FILE) != original_sha:
        sys.stderr.write(
            "SELF TEST FAILED: post-restore SHA does not match original. "
            "The script may have a bug; investigate before submitting.\n"
        )
        return 1

    sys.stdout.write("SELF TEST PASSED: gate fired on the deliberate regression.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
