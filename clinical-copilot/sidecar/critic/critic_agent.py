"""Critic agent — Phase 11 extension.

Receives a verified ``ResponsePacket`` and re-checks it against four
clinical-safety rules:

1. **Uncited claims** — every clinical claim must carry a citation.
   The verifier already enforces this; the critic is the second line
   of defense in case a verifier rule edit lets one through.
2. **Unsafe action recommendations** — any "increase dose", "stop
   medication", "switch to" recommendation must trace to a guideline
   chunk. Recommendations grounded only in chart facts (without a
   guideline cite) are flagged.
3. **Allergy/medication contradictions** — the patient's allergy list
   versus the patient's medication list. A penicillin allergy plus an
   amoxicillin prescription is the canonical example.
4. **Audience tone** — recommendations addressed to a non-clinician
   audience (e.g. "you should take") in a clinician-facing tool are
   flagged.

A claim flagged by the critic is returned to the supervisor with the
flag attached; the supervisor either runs additional retrieval or
returns a refusal. The critic is OFF by default (extension flag); the
graph wires it in when ``GraphNodes.critic`` is non-None.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final

from sidecar.agents.w2.state import ClinicalClaim, ResponsePacket


logger = logging.getLogger(__name__)


CRITIC_PROMPT_VERSION: Final[str] = "critic.v1"


# Action-recommendation cues. Loose matches; the critic prefers false
# positives (flagging something benign) over false negatives (letting
# an unsafe recommendation through).
_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(start|stop|increase|decrease|switch|change|add|discontinue|titrate)\b",
    re.IGNORECASE,
)

# Tone cues that indicate patient-facing language ("you should...").
_PATIENT_TONE_RE: Final[re.Pattern[str]] = re.compile(
    r"\byou (should|need to|must)\b|\bplease take\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CriticFinding:
    """One critic flag against a specific claim."""

    claim_text: str
    rule: str  # one of: 'uncited', 'unsafe_action', 'contradiction', 'patient_tone'
    detail: str


@dataclass(frozen=True)
class CriticReport:
    """Output of a critic invocation. Empty findings list = clean."""

    findings: list[CriticFinding]
    prompt_version: str

    @property
    def is_clean(self) -> bool:
        return not self.findings


def review(
    packet: ResponsePacket,
    *,
    patient_allergies: list[str],
    patient_medications: list[str],
    guideline_citation_prefixes: list[str] | None = None,
) -> CriticReport:
    """Run all four critic rules against the response packet.

    ``patient_allergies`` and ``patient_medications`` are the strings
    the critic searches for in the response; pass an empty list to
    disable the contradiction check (e.g. when the patient's chart is
    incomplete and the gap should not be reported as a contradiction).
    """
    findings: list[CriticFinding] = []
    guideline_prefixes = guideline_citation_prefixes or [
        "ADA-", "AHA-", "USPSTF-", "ACR-", "NOF-", "CDC-", "AAFP-",
    ]

    for claim in packet.claims:
        # Rule 1: uncited (defense in depth — verifier handles primary).
        if not claim.citations:
            findings.append(
                CriticFinding(
                    claim_text=claim.text,
                    rule="uncited",
                    detail="no citations on claim; verifier should have dropped it.",
                )
            )

        # Rule 2: unsafe action without guideline citation.
        if _ACTION_RE.search(claim.text):
            grounded = any(
                cite.startswith(prefix)
                for cite in claim.citations
                for prefix in guideline_prefixes
            )
            if not grounded:
                findings.append(
                    CriticFinding(
                        claim_text=claim.text,
                        rule="unsafe_action",
                        detail=(
                            "action recommendation lacks a guideline citation; "
                            "every dose change must trace to ADA / AHA / USPSTF / etc."
                        ),
                    )
                )

        # Rule 3: contradiction. Loose match: every allergy whose name
        # appears in the response, where the response also mentions a
        # medication that overlaps the allergy's drug class, is a flag.
        # We check name overlap only here; clinical class mapping
        # (penicillin -> amoxicillin) is a Phase 12 enrichment.
        text_lower = claim.text.lower()
        for allergy in patient_allergies:
            if allergy.lower() in text_lower:
                for med in patient_medications:
                    if med.lower() in text_lower and med.lower() != allergy.lower():
                        if _shares_class(allergy, med):
                            findings.append(
                                CriticFinding(
                                    claim_text=claim.text,
                                    rule="contradiction",
                                    detail=(
                                        f"allergy={allergy!r} appears alongside "
                                        f"medication={med!r}; possible class overlap."
                                    ),
                                )
                            )

        # Rule 4: patient-facing tone in a clinician-facing response.
        if _PATIENT_TONE_RE.search(claim.text):
            findings.append(
                CriticFinding(
                    claim_text=claim.text,
                    rule="patient_tone",
                    detail="claim addresses the patient directly; rewrite in clinician voice.",
                )
            )

    return CriticReport(findings=findings, prompt_version=CRITIC_PROMPT_VERSION)


_KNOWN_CLASSES: Final[list[tuple[str, list[str]]]] = [
    ("penicillin", ["penicillin", "amoxicillin", "ampicillin", "augmentin", "piperacillin"]),
    ("sulfa", ["sulfa", "sulfamethoxazole", "bactrim", "tmp-smx"]),
    ("nsaid", ["nsaid", "ibuprofen", "naproxen", "ketorolac", "diclofenac"]),
    ("statin", ["statin", "atorvastatin", "simvastatin", "rosuvastatin"]),
]


def _shares_class(allergy: str, med: str) -> bool:
    a = allergy.lower()
    m = med.lower()
    for _, members in _KNOWN_CLASSES:
        if any(name in a for name in members) and any(name in m for name in members):
            return True
    return False


__all__ = [
    "CRITIC_PROMPT_VERSION",
    "CriticFinding",
    "CriticReport",
    "review",
]
