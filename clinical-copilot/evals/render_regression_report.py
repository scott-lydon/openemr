"""Render a markdown regression report from a JUnit XML eval result.

Used by the w2-eval-gate workflow on failure to surface failing cases in
the GitHub PR step summary. Sorts cases by rubric, then by case ID, and
prints the failure message and the case rationale (loaded from
``cases.jsonl``) for each. Exits zero unconditionally; this is a render
script, not a gate.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CASES_PATH = Path(__file__).parent / "golden_w2" / "cases.jsonl"


def load_cases() -> dict[str, dict]:
    if not CASES_PATH.is_file():
        return {}
    out: dict[str, dict] = {}
    with CASES_PATH.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            case = json.loads(line)
            out[case["id"]] = case
    return out


def render(junit_path: Path) -> str:
    if not junit_path.is_file():
        return f"# Regression Report\n\n_No JUnit file at `{junit_path}`._\n"

    tree = ET.parse(junit_path)
    cases = load_cases()
    failing: list[tuple[str, str, str, str]] = []  # (rubric, case_id, message, rationale)

    for case in tree.iter("testcase"):
        failure = case.find("failure") or case.find("error")
        if failure is None:
            continue
        case_id = case.attrib.get("name", "<unknown>")
        rubric = "_unknown"
        properties = case.find("properties")
        if properties is not None:
            for prop in properties.iter("property"):
                if prop.attrib.get("name") == "rubric":
                    rubric = prop.attrib.get("value", "_unknown")
                    break
        message = (failure.attrib.get("message") or "").splitlines()[0][:300]
        rationale = ""
        # Pytest parametrized IDs include the case ID in brackets.
        for cid, c in cases.items():
            if cid in case_id:
                rationale = c.get("rationale", "")
                break
        failing.append((rubric, case_id, message, rationale))

    if not failing:
        return "# Regression Report\n\n**No failing cases.**\n"

    failing.sort()
    lines = [
        "# Regression Report",
        "",
        f"**{len(failing)} failing case(s).** Each failure links the rubric "
        "category it violated, the case identifier, the failure message, and "
        "the case rationale (so the reviewer knows what failure mode the case "
        "was protecting).",
        "",
        "| Rubric | Case ID | Failure | Rationale |",
        "|---|---|---|---|",
    ]
    for rubric, case_id, message, rationale in failing:
        msg_cell = message.replace("|", "\\|")
        rat_cell = rationale.replace("|", "\\|")
        lines.append(f"| `{rubric}` | `{case_id}` | {msg_cell} | {rat_cell} |")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    junit_path = Path(args[0]) if args else Path("eval-results.xml")
    sys.stdout.write(render(junit_path))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
