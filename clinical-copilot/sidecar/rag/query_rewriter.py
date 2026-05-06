"""Query rewriter — synonym and abbreviation expansion.

The retriever's input is the user's literal question; clinical
guidelines use canonical terminology that often differs from the way a
clinician phrases a follow-up. The rewriter expands abbreviations
("A1c" → "HbA1c, glycated hemoglobin"), adds canonical synonyms
("MI" → "myocardial infarction"), and leaves the rest untouched.

Constraint: the rewriter does NOT add inferred clinical context. A
question about "elevated cholesterol" does not get expanded to "LDL,
HDL, total cholesterol" because that is interpretation, not
abbreviation expansion. Adding interpretation here would make the
retriever fish for content the user did not actually ask about.

Two implementations:

- ``DictionaryRewriter`` — the default. Tokenizes the query, looks up
  each token in a curated abbreviation dictionary, emits the original
  query plus the appended canonical forms.
- ``LlmRewriter`` — Phase 11 contextual rewriter that sees the
  patient's active problem list and tailors the expansion. Out of
  scope for Phase 4; the protocol is here so the swap-in is a one-line
  change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final, Mapping, Protocol


# Curated dictionary of clinical abbreviations. Each entry maps the
# abbreviation to a list of canonical synonyms. The dictionary is
# intentionally short — only abbreviations whose expansion is
# unambiguous appear here. Abbreviations with multiple meanings (PE
# could be "pulmonary embolism" or "physical exam") are left out.
DEFAULT_ABBREVIATION_DICTIONARY: Final[Mapping[str, list[str]]] = {
    "a1c": ["HbA1c", "glycated hemoglobin"],
    "hba1c": ["glycated hemoglobin"],
    "bp": ["blood pressure", "hypertension"],
    "cv": ["cardiovascular"],
    "cvd": ["cardiovascular disease"],
    "ldl": ["low density lipoprotein", "LDL cholesterol"],
    "hdl": ["high density lipoprotein", "HDL cholesterol"],
    "mi": ["myocardial infarction", "heart attack"],
    "cad": ["coronary artery disease"],
    "ckd": ["chronic kidney disease"],
    "egfr": ["estimated glomerular filtration rate"],
    "hf": ["heart failure"],
    "afib": ["atrial fibrillation"],
    "copd": ["chronic obstructive pulmonary disease"],
    "uspstf": ["United States Preventive Services Task Force"],
    "ada": ["American Diabetes Association"],
    "aha": ["American Heart Association"],
    "phq-9": ["Patient Health Questionnaire-9", "depression screening"],
    "gad-7": ["Generalized Anxiety Disorder-7", "anxiety screening"],
    "dxa": ["dual-energy X-ray absorptiometry", "bone density scan"],
    "rxnorm": ["RxNorm"],
    "loinc": ["Logical Observation Identifiers Names and Codes"],
    "icd": ["International Classification of Diseases"],
}


# Prompt version recorded on every rewrite so a regression after a
# dictionary edit is attributable.
DICTIONARY_REWRITER_VERSION: Final[str] = "rewrite.dict.v1"

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


@dataclass(frozen=True)
class RewriteResult:
    """Output of a rewriter call.

    The original query is preserved as the first line so a downstream
    BM25 search keeps the user's literal phrasing in the top-N. The
    rewritten suffix appends synonyms separated by spaces; the BM25
    tokenizer splits them naturally.
    """

    original: str
    rewritten: str
    version: str
    expansions_applied: list[tuple[str, list[str]]]


class QueryRewriter(Protocol):
    """Protocol every rewriter implementation honors."""

    async def rewrite(self, query: str) -> RewriteResult:
        ...


@dataclass
class DictionaryRewriter:
    """Default rewriter — pure-Python, deterministic, offline.

    The dictionary is injected so a test can substitute a tiny one to
    exercise the rewriter shape without coupling to the production
    list.
    """

    dictionary: Mapping[str, list[str]] = field(
        default_factory=lambda: DEFAULT_ABBREVIATION_DICTIONARY
    )
    version: str = DICTIONARY_REWRITER_VERSION

    async def rewrite(self, query: str) -> RewriteResult:
        if not query.strip():
            return RewriteResult(
                original=query,
                rewritten=query,
                version=self.version,
                expansions_applied=[],
            )

        seen: set[str] = set()
        expansions: list[tuple[str, list[str]]] = []
        synonym_tokens: list[str] = []

        for token in _TOKEN_RE.findall(query):
            normalized = token.lower()
            if normalized in seen:
                continue
            synonyms = self.dictionary.get(normalized)
            if not synonyms:
                continue
            seen.add(normalized)
            expansions.append((token, list(synonyms)))
            synonym_tokens.extend(synonyms)

        if not synonym_tokens:
            return RewriteResult(
                original=query,
                rewritten=query,
                version=self.version,
                expansions_applied=[],
            )

        rewritten = f"{query.strip()} {' '.join(synonym_tokens)}"
        return RewriteResult(
            original=query,
            rewritten=rewritten,
            version=self.version,
            expansions_applied=expansions,
        )


__all__ = [
    "DEFAULT_ABBREVIATION_DICTIONARY",
    "DICTIONARY_REWRITER_VERSION",
    "DictionaryRewriter",
    "QueryRewriter",
    "RewriteResult",
]
