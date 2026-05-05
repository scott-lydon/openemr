"""Verifier: citation contract enforcement + Personal Health Information
(PHI) scrub.

Three rules:

1. **Every clinical claim must carry at least one valid citation.**
   The DTO already enforces ``min_length=1`` at parse time; the
   verifier additionally checks that each citation id resolves to a
   known source (a chunk in the corpus or a document in the upload
   queue's record). A claim with an unresolved citation is dropped.

2. **No PHI in any response field.** Scans the rendered ``summary``
   and every ``ClinicalClaim.text`` for patterns matching Social
   Security Numbers, MRNs, and date-of-birth strings. A match
   replaces the offending substring with ``[REDACTED:<kind>]``. The
   number of replacements is recorded in ``trace_attributes``.

3. **Fail closed when the PHI detector is unavailable.** If
   ``presidio-analyzer`` is missing or fails, the verifier raises
   ``PresidioUnavailable`` rather than falling through. The graph
   gateway maps this to a 503 so the gateway never silently emits a
   response that bypassed the PHI gate.

Why a separate verifier rather than inlining checks:

- The citation-contract enforcement and the PHI scrub are
  cross-cutting concerns; every response goes through them. A separate
  module makes the contract one place's responsibility.
- The verifier writes ``verifier.dropped_claims_count`` and
  ``verifier.phi_leak_blocked`` as span attributes; both are panels on
  the Phase 10 dashboard.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final, Iterable, Protocol

from sidecar.agents.w2.state import ClinicalClaim, GraphState, ResponsePacket


logger = logging.getLogger(__name__)


# Patterns the lightweight in-process scrubber catches. Production also
# runs Presidio if available; the patterns here are the safety net for
# the "Presidio missing" case (we still scrub, just less thoroughly).
_PHI_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("mrn", re.compile(r"\bMRN[0-9]{4,}\b", re.IGNORECASE)),
    ("dob", re.compile(r"\bDOB:\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", re.IGNORECASE)),
    ("phone", re.compile(r"\b\d{3}-\d{3}-\d{4}\b")),
    ("patient_ref", re.compile(r"Patient/[a-fA-F0-9-]{4,}")),
)


class PresidioUnavailable(Exception):
    """The PHI detector could not run.

    Verifier fails closed; gateway maps to 503 with a typed error
    explaining that Presidio is not reachable. Operator's runbook entry
    points at the spaCy model + Presidio install path.
    """


class CitationResolver(Protocol):
    """Protocol the verifier uses to check that a citation id is real.

    Production implementation queries the ``citations`` table from
    Phase 6 plus the corpus' ``guideline_chunks`` table. Tests inject a
    set-backed stub.
    """

    def resolve(self, citation_id: str) -> bool:
        ...


@dataclass(frozen=True)
class StubCitationResolver:
    """Set-backed resolver for unit tests."""

    known_ids: frozenset[str]

    def resolve(self, citation_id: str) -> bool:
        return citation_id in self.known_ids


@dataclass
class VerifierConfig:
    """Knobs the verifier honors.

    ``allow_in_process_scrub_only`` lets callers opt out of Presidio for
    unit tests where spinning up the spaCy model is overkill; production
    keeps this False so a missing Presidio fails closed.
    """

    allow_in_process_scrub_only: bool = False


def verify(
    state: GraphState,
    *,
    resolver: CitationResolver,
    config: VerifierConfig | None = None,
) -> ResponsePacket:
    """Run all three verifier rules. Return a ResponsePacket.

    Raises ``PresidioUnavailable`` when Presidio is required but not
    installed. The caller maps to a 503.
    """
    cfg = config or VerifierConfig()

    # 1. Drop claims with unresolved citations.
    kept_claims: list[ClinicalClaim] = []
    dropped = 0
    for claim in state.raw_claims:
        if all(resolver.resolve(cid) for cid in claim.citations):
            kept_claims.append(claim)
        else:
            dropped += 1

    # 2. Scrub PHI in claim text and accumulate counts.
    redacted_total = 0
    scrubbed_claims: list[ClinicalClaim] = []
    for claim in kept_claims:
        scrubbed_text, count = _scrub_phi_in_process(claim.text)
        redacted_total += count
        scrubbed_claims.append(
            ClinicalClaim(text=scrubbed_text, citations=list(claim.citations))
        )

    # Optional second pass through Presidio for richer detection.
    if not cfg.allow_in_process_scrub_only:
        try:
            scrubbed_claims, presidio_redactions = _presidio_scrub(scrubbed_claims)
            redacted_total += presidio_redactions
        except ImportError as exc:
            raise PresidioUnavailable(
                "Presidio is not installed but allow_in_process_scrub_only is "
                "False. Install presidio-analyzer + presidio-anonymizer + the "
                "en_core_web_lg spaCy model, OR set "
                "VerifierConfig(allow_in_process_scrub_only=True) for unit "
                "tests where the in-process regex sweep is acceptable."
            ) from exc
        except Exception as exc:
            raise PresidioUnavailable(
                f"Presidio raised {type(exc).__name__}: {exc!s}"
            ) from exc

    # Refusal handling: if every claim was dropped, the agent refuses
    # rather than fabricating a response.
    refusal_reason: str | None = None
    if state.raw_claims and not scrubbed_claims:
        refusal_reason = (
            "Every candidate claim failed the citation contract; the agent "
            "refuses to surface unverified content."
        )

    summary = _summarize(state.user_question, scrubbed_claims, refusal_reason)
    summary, summary_redactions = _scrub_phi_in_process(summary)
    redacted_total += summary_redactions

    return ResponsePacket(
        summary=summary,
        claims=scrubbed_claims,
        refusal_reason=refusal_reason,
        trace_attributes={
            "verifier.dropped_claims_count": dropped,
            "verifier.phi_redactions_total": redacted_total,
            "verifier.phi_leak_blocked": redacted_total > 0,
            "verifier.refused": refusal_reason is not None,
        },
    )


def _scrub_phi_in_process(text: str) -> tuple[str, int]:
    """Apply the regex-based PHI scrubber. Returns ``(scrubbed, count)``."""
    if not text:
        return text, 0
    out = text
    count = 0
    for kind, pattern in _PHI_PATTERNS:
        new_out, n = pattern.subn(f"[REDACTED:{kind}]", out)
        out = new_out
        count += n
    return out, count


def _presidio_scrub(claims: list[ClinicalClaim]) -> tuple[list[ClinicalClaim], int]:
    """Run Presidio over each claim's text. Returns (scrubbed, count).

    Imports lazily so the module loads without Presidio installed.
    """
    from presidio_analyzer import AnalyzerEngine  # type: ignore[import-untyped]
    from presidio_anonymizer import AnonymizerEngine  # type: ignore[import-untyped]

    analyzer = AnalyzerEngine()
    anonymizer = AnonymizerEngine()
    scrubbed: list[ClinicalClaim] = []
    total = 0
    for claim in claims:
        results = analyzer.analyze(text=claim.text, language="en")
        if not results:
            scrubbed.append(claim)
            continue
        anonymized = anonymizer.anonymize(text=claim.text, analyzer_results=results)
        scrubbed.append(
            ClinicalClaim(text=anonymized.text, citations=list(claim.citations))
        )
        total += len(results)
    return scrubbed, total


def _summarize(
    user_question: str,
    claims: Iterable[ClinicalClaim],
    refusal_reason: str | None,
) -> str:
    """Trivial summary stub.

    The Jinja-based response formatter (``response_formatter.py``)
    produces the user-facing message; this summary is the structured
    text the formatter starts from. Keeping the verifier's summary
    minimal means the verifier has no template logic.
    """
    if refusal_reason:
        return refusal_reason
    claim_list = list(claims)
    if not claim_list:
        return "No claims surfaced for: " + user_question
    return f"Found {len(claim_list)} verified claim(s) for: {user_question}"


__all__ = [
    "CitationResolver",
    "PresidioUnavailable",
    "StubCitationResolver",
    "VerifierConfig",
    "verify",
]
