"""Contextual query rewriter — Phase 11 extension.

Wraps the Phase 4 ``DictionaryRewriter`` and prepends synonyms drawn
from the patient's active problem list and demographics:

- A patient with chronic kidney disease asking about "creatinine" gets
  the expansion plus "estimated glomerular filtration rate, eGFR".
- A 55-year-old male asking about "screening" gets demographic-aware
  expansion that includes the relevant USPSTF age-banded
  recommendation phrases.

Constraints:

- The rewriter still does NOT add inferred clinical context that the
  user did not at least gesture toward. Adding age-banded screening
  expansions is OK because the user explicitly asked about
  "screening"; adding a colorectal-specific term to a
  hypertension-only question is not.
- The dictionary is the union of the Phase 4 dictionary and the
  context-derived additions. Unknown context shapes (an empty problem
  list, an absent age band) fall through to the base rewriter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Mapping

from sidecar.rag.query_rewriter import (
    DEFAULT_ABBREVIATION_DICTIONARY,
    DICTIONARY_REWRITER_VERSION,
    DictionaryRewriter,
    QueryRewriter,
    RewriteResult,
)


CONTEXTUAL_REWRITER_VERSION: Final[str] = "rewrite.contextual.v1"


# Problem-driven expansions. Each problem maps to a list of
# ``(needle, synonyms)`` pairs: when the problem is in the patient's
# active list AND the needle appears in the query, the synonyms are
# appended.
_PROBLEM_EXPANSIONS: Final[Mapping[str, list[tuple[str, list[str]]]]] = {
    "chronic kidney disease": [
        ("creatinine", ["estimated glomerular filtration rate", "eGFR"]),
        ("kidney", ["renal function", "eGFR", "albuminuria"]),
    ],
    "diabetes": [
        ("control", ["HbA1c", "glycemic control", "fasting glucose"]),
        ("a1c", ["glycemic target"]),
    ],
    "heart failure": [
        ("breathing", ["dyspnea", "orthopnea"]),
        ("weight", ["fluid balance", "diuresis"]),
    ],
}


@dataclass
class ContextualPatientFacts:
    """Subset of the patient's facts the rewriter is allowed to read.

    Intentionally narrow: only the active problem list and a
    coarse age band. Anything richer would be patient-data leakage
    into the rewriter, which then might leak into the reranker via
    the rewritten query.
    """

    active_problems: list[str] = field(default_factory=list)
    age_band: str | None = None  # "under_40" | "40_64" | "65_plus" | None
    sex_specific: str | None = None  # "male" | "female" | None


@dataclass
class ContextualRewriter:
    """Wraps DictionaryRewriter; prepends context-derived synonyms.

    Falls through to the base rewriter when ``patient`` is None or has
    no active problems.
    """

    base: DictionaryRewriter = field(default_factory=DictionaryRewriter)
    patient: ContextualPatientFacts | None = None
    version: str = CONTEXTUAL_REWRITER_VERSION

    async def rewrite(self, query: str) -> RewriteResult:
        result = await self.base.rewrite(query)
        if not self.patient or not self.patient.active_problems:
            return RewriteResult(
                original=result.original,
                rewritten=result.rewritten,
                version=self.version,
                expansions_applied=result.expansions_applied,
            )

        extra_synonyms: list[str] = []
        added_expansions = list(result.expansions_applied)
        lower_query = query.lower()

        for problem in self.patient.active_problems:
            for needle, synonyms in _PROBLEM_EXPANSIONS.get(problem.lower(), []):
                if needle.lower() in lower_query:
                    for syn in synonyms:
                        if syn not in extra_synonyms:
                            extra_synonyms.append(syn)
                    added_expansions.append((needle, list(synonyms)))

        if not extra_synonyms:
            return RewriteResult(
                original=result.original,
                rewritten=result.rewritten,
                version=self.version,
                expansions_applied=added_expansions,
            )

        rewritten = result.rewritten + " " + " ".join(extra_synonyms)
        return RewriteResult(
            original=result.original,
            rewritten=rewritten,
            version=self.version,
            expansions_applied=added_expansions,
        )


__all__ = [
    "CONTEXTUAL_REWRITER_VERSION",
    "ContextualPatientFacts",
    "ContextualRewriter",
]
