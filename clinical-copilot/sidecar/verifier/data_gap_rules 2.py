"""Data-gap rule knowledge base.

Replaces the hand-rolled ``if gout_problem...`` / ``if dm_problem...``
chain that previously lived in :mod:`verifier` with a data-driven
table. Every gap surfaced to the clinician is the result of one rule
in this list firing on a patient whose snapshot matches both
predicates, so adding a new condition / required-lab pairing is a
one-line change here — no edits to verifier code or tests.

Each :class:`DataGapRule` is intentionally small: condition keywords,
required-lab keywords, and a message template that gets formatted
with the patient's actual problem label and the recent-labs window
phrase. The verifier walks the rules in declaration order and emits
at most two gaps so the UI stays uncluttered (ARCHITECTURE.md §10).

If the rule list grows past a handful of entries, externalise it to
YAML and load at startup; the dataclass shape below is intentionally
serialisation-friendly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DataGapRule:
    """One condition→missing-lab gap rule.

    ``condition_keywords``: substring needles matched against an
    active-problem label, lowercased. A rule fires when ANY needle
    matches AND no recent lab matches ``required_lab_keywords``.

    ``message_template`` is formatted with two named arguments:
    ``problem`` (the matched problem label, verbatim) and ``window``
    (the lab-window phrase from the verifier).
    """

    condition_keywords: tuple[str, ...]
    required_lab_keywords: tuple[str, ...]
    message_template: str


# The rule list. Keep one entry per (condition, required lab) pairing
# the team has agreed is worth surfacing as a data gap. This list
# starts conservative — the eval suite (layer 2) covers exactly these
# rules, so adding a new entry should always come with a
# corresponding eval case.
DATA_GAP_RULES: tuple[DataGapRule, ...] = (
    DataGapRule(
        condition_keywords=("gout",),
        required_lab_keywords=("urate", "uric acid"),
        message_template=(
            "no recent uric acid measured for active problem "
            "“{problem}” — would resolve gout vs infection"
        ),
    ),
    DataGapRule(
        condition_keywords=("diabetes",),
        required_lab_keywords=("hba1c", "a1c", "hemoglobin a1c"),
        message_template=(
            "no HbA1c measured for active problem “{problem}” {window}"
        ),
    ),
)
