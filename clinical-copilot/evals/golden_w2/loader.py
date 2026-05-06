"""Load every golden eval case from ``cases/`` and ``seed_cases.jsonl``.

Each .jsonl file holds one case per line. The loader:

1. Walks every ``.jsonl`` file under ``evals/golden_w2/`` (recursive).
2. Parses each line as one ``GoldenCase``. A ValidationError is fatal
   to the whole loader — a malformed case would otherwise be skipped
   silently and the test would underreport coverage.
3. Detects duplicate ``id`` across files (a copy-paste bug); raises
   ValueError with both file paths so the operator knows where to fix.

Returns a list sorted by id so the harness's run order is stable
across machines.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.golden_w2.schemas.case import GoldenCase


def load_cases(root: Path) -> list[GoldenCase]:
    """Load every case under ``root``.

    ``root`` is the ``evals/golden_w2`` directory. The function returns
    a list sorted by case id; duplicate ids raise. The order is stable
    so a regression report names cases the same way every run.
    """
    if not root.is_dir():
        raise FileNotFoundError(
            f"golden eval root not found: {root!r}; ensure the path is the "
            "evals/golden_w2 directory."
        )

    cases_by_id: dict[str, tuple[GoldenCase, Path]] = {}
    for jsonl_path in sorted(root.rglob("*.jsonl")):
        for line_index, line in enumerate(_read_lines(jsonl_path), start=1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{jsonl_path}:{line_index} JSON decode failed: {exc!s}"
                ) from exc
            try:
                case = GoldenCase.model_validate(payload, strict=False)
            except Exception as exc:
                raise ValueError(
                    f"{jsonl_path}:{line_index} case validation failed: "
                    f"{type(exc).__name__}: {exc!s}"
                ) from exc
            existing = cases_by_id.get(case.id)
            if existing is not None:
                _, existing_path = existing
                raise ValueError(
                    f"duplicate case id {case.id!r} in {jsonl_path} (also in "
                    f"{existing_path})"
                )
            cases_by_id[case.id] = (case, jsonl_path)

    return [case for case, _ in sorted(cases_by_id.values(), key=lambda pair: pair[0].id)]


def _read_lines(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                yield stripped


__all__ = ["load_cases"]
