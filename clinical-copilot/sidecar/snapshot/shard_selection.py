"""Selective FHIR shard retrieval.

Background. ``DEFAULT_RESOURCE_QUERIES`` in ``fhir_client.py`` defines
eight per-resource FHIR endpoints (Condition, MedicationRequest,
AllergyIntolerance, Observation × {vital-signs, laboratory}, Encounter,
Procedure, DocumentReference). The original sidecar wiring fanned out
to all eight on every ``/chat`` call. That is the right shape for the
first pre-visit cross-check (Use Case A) — we genuinely need every
documented finding before we can pair-judge them — but it is wasteful
for chart-error scans (no need to pull the documents shard) and
actively wrong for the mid-visit follow-up (Use Case C: the clinician
asks "when was her last colonoscopy?" and we should pull
``Procedure`` / ``DocumentReference`` and skip vitals/labs).

This module defines the shard selection policy in one place so the
chat handler doesn't have to know about FHIR query templates and the
FHIR client doesn't have to know about purposes or natural-language
messages. The policy is two layers:

1. ``select_shards_for_purpose`` returns the baseline shard set for a
   purpose. ``diagnostic_cross_check`` keeps every shard (the pairwise
   comparator's accuracy depends on a complete chart). The other
   purposes drop shards that add latency without clinical value.
2. ``refine_shards_for_message`` adds shards that the message text
   suggests are needed (e.g. "colonoscopy" → ``procedures`` +
   ``documents``). Refinement only adds, never removes — a follow-up
   question that mentions nothing recognisable still gets the
   purpose-default set.

The keyword table is conservative — anything we do not recognise stays
on the default set, so the policy can never *under*-fetch the data the
agent needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .fhir_client import DEFAULT_RESOURCE_QUERIES

ChatPurpose = Literal[
    "diagnostic_cross_check",
    "chart_error_scan",
    "follow_up_question",
]


# Stable order so the fan-out result map iterates the same way every
# call (deterministic snapshots help debugging and the audit log).
_ALL_SHARD_NAMES: tuple[str, ...] = tuple(name for name, _ in DEFAULT_RESOURCE_QUERIES)


@dataclass(frozen=True)
class ShardSelection:
    """Which FHIR resource shards the snapshot fetch should pull.

    ``names`` is a frozenset for membership checks; ``ordered`` is the
    materialised query tuple in ``DEFAULT_RESOURCE_QUERIES`` order so
    the fan-out preserves stable telemetry keys.
    """

    names: frozenset[str]

    @property
    def ordered(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (name, template)
            for name, template in DEFAULT_RESOURCE_QUERIES
            if name in self.names
        )

    def __iter__(self):
        return iter(self.ordered)

    def __len__(self) -> int:
        return len(self.names)

    def __contains__(self, name: object) -> bool:
        return name in self.names

    def union(self, *others: "ShardSelection") -> "ShardSelection":
        merged = set(self.names)
        for other in others:
            merged |= other.names
        return ShardSelection(names=frozenset(merged))


# All shards. The fallback when we cannot make a smaller pick safely.
ALL_SHARDS: ShardSelection = ShardSelection(names=frozenset(_ALL_SHARD_NAMES))


# Per-purpose baselines. Values are the shard *names* (not query
# templates) — kept here so a future schema change in
# ``DEFAULT_RESOURCE_QUERIES`` does not silently drop a shard.
_PURPOSE_BASELINES: dict[str, frozenset[str]] = {
    # Pre-visit cross-check: pair generator A pairs every presenting
    # symptom against every documented finding. Dropping any shard
    # would suppress real candidate explanations from the chart.
    "diagnostic_cross_check": frozenset(_ALL_SHARD_NAMES),
    # Chart-error scan: the comparator runs over (finding × finding)
    # for inconsistencies. Documents add bulk without
    # rule-store-recognised inconsistency content; drop them by
    # default. The keyword refiner can add them back for a query like
    # "scan the radiology notes".
    "chart_error_scan": frozenset(
        {"conditions", "medications", "allergies", "vitals", "labs",
         "procedures", "encounters"},
    ),
    # Mid-visit follow-up: the cheapest sensible default is the
    # problem list + active meds + allergies — every follow-up
    # question touches at least one of those. Specific shards (labs,
    # procedures, …) are added back by the message-keyword refiner.
    "follow_up_question": frozenset({"conditions", "medications", "allergies"}),
}


# Keyword → shards mapping for follow-up refinement. Patterns are
# substring-tolerant (``re.search`` on a lowercased message). Each
# entry uses ``\b`` only at the START of the stem so a prefix like
# ``colonosc`` matches "colonoscopy", "colonoscopies", etc. Anchoring
# with ``\b`` at both ends would require a word boundary mid-word and
# break stem matching — that bug bit during development of this
# selector and the comment is here so it does not bite again.
# Keep the patterns broad enough to catch clinician shorthand ("a1c",
# "echo") and the underlying clinical English. When in doubt, match —
# over-fetching is cheap, under-fetching breaks the answer.
_KEYWORD_TO_SHARDS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), frozenset(shards))
    for pattern, shards in (
        # Labs
        (r"\b(?:lab|labs|a1c|hba1c|glucose|creatinine|bun|gfr|cbc|cmp|"
         r"lipid|cholesterol|tsh|vitamin\s*[abd]|crp|esr|inr|"
         r"potassium|sodium|hemoglob\w*|hgb|wbc|platelet\w*|"
         r"uric\s*acid|ferritin|troponin|bnp)",
         {"labs"}),
        # Vitals
        (r"\b(?:vital\w*|blood\s*pressure|bp|heart\s*rate|"
         r"hr|pulse|temperature|temp|spo2|oxygen|sat|"
         r"weight|bmi|height|respiratory\s*rate|rr)",
         {"vitals"}),
        # Procedures (interventional). Prefix stems intentionally
        # match longer words: "colonosc" → "colonoscopy", "endosc" →
        # "endoscopy", "laparoscop" → "laparoscopic", etc.
        (r"\b(?:colonosc\w*|endosc\w*|biopsy|biopsies|surgery|"
         r"surgical|operation|operative|"
         r"laparoscop\w*|arthroscop\w*|catheter\w*|angioplast\w*|"
         r"resect\w*|excis\w*|removal|implant\w*)",
         {"procedures"}),
        # Documents / notes / referrals — anything that would live
        # on DocumentReference in OpenEMR's FHIR surface.
        (r"\b(?:note|notes|consult\w*|referral|specialist|cardiolog\w*|"
         r"neurolog\w*|orthoped\w*|gastro\w*|nephrolog\w*|endocrin\w*|"
         r"discharge|h&p|history\s*and\s*physical|"
         r"radiolog\w*|imaging|x-ray|xray|mri|ct\s*scan|ultrasound|"
         r"echo|echocardio\w*|ekg|ecg)",
         {"documents"}),
        # Encounters (visits in time)
        (r"\b(?:visit\w*|encounter\w*|appointment\w*|"
         r"admission|admitted|hospitaliz\w*|er\s*visit|"
         r"emergency\s*(?:room|department)|urgent\s*care|"
         r"last\s*seen|last\s*visit|previous\s*visit|prior\s*visit)",
         {"encounters"}),
        # Procedures + documents — radiology results often live in
        # both DocumentReference and Procedure depending on capture.
        (r"\b(?:stent|pacemaker|defibrillator|fracture|cast|"
         r"injection\w*|infusion\w*|transfusion\w*)",
         {"procedures", "documents"}),
    )
)


def select_shards_for_purpose(purpose: ChatPurpose) -> ShardSelection:
    """Return the per-purpose baseline shard set.

    Raises ``ValueError`` for unknown purposes so a typo never silently
    fans out the full eight-shard set.
    """
    if purpose not in _PURPOSE_BASELINES:
        raise ValueError(
            f"unknown chat purpose {purpose!r}; expected one of "
            f"{sorted(_PURPOSE_BASELINES)}"
        )
    return ShardSelection(names=_PURPOSE_BASELINES[purpose])


def refine_shards_for_message(
    base: ShardSelection, message: str | None
) -> ShardSelection:
    """Add shards suggested by keywords in ``message``.

    Refinement is additive: this never *removes* a shard the baseline
    requested. An empty or None message returns the baseline unchanged.
    A message with no recognised keywords also returns the baseline
    unchanged (better to under-add than under-fetch — if the message
    used a term we don't recognise, we still have the purpose's
    baseline data to reason over).
    """
    if not message or not message.strip():
        return base
    msg = message.lower()
    extra: set[str] = set()
    for pattern, shards in _KEYWORD_TO_SHARDS:
        if pattern.search(msg):
            extra |= shards
    if not extra:
        return base
    return ShardSelection(names=base.names | extra)


def select_shards(purpose: ChatPurpose, message: str | None = None) -> ShardSelection:
    """Top-level entry point: per-purpose baseline + message refinement.

    The chat handler calls this once per request and hands the result
    to ``SnapshotService.build(... shards=...)``. The result's
    ``ordered`` property is what the FHIR client expects.
    """
    return refine_shards_for_message(select_shards_for_purpose(purpose), message)
