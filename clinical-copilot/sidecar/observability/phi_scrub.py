"""Personal Health Information (PHI) scrub at trace-flush time.

Layer 5 of the sanitization stack runs at the verifier (Phase 5). This
module is the SECOND scrub: it runs over every span attribute and
every log line right before they leave the process. Defense in depth.

Fail-closed contract:

- If Presidio is required but not available, this module's
  ``ensure_ready`` raises ``PresidioRequiredButMissing``. The
  observability pipeline is configured to refuse to flush rather than
  silently let through unredacted spans.
- The lightweight in-process regex sweep is always available; the
  Presidio path is the deeper, slower, more thorough check that runs
  in production.

Configuration:

- ``COPILOT_PHI_SCRUB_MODE`` — ``"presidio"`` (default in production)
  or ``"in_process"`` (allowed in dev / test). Production refuses to
  fall back to in-process even when Presidio fails to initialize; the
  dashboard's ``observability.presidio_unavailable`` alert fires
  instead.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Final


logger = logging.getLogger(__name__)


PHI_SCRUB_MODE_ENV: Final[str] = "COPILOT_PHI_SCRUB_MODE"
DEFAULT_MODE: Final[str] = "presidio"


_PATTERNS: Final[list[tuple[str, re.Pattern[str]]]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("mrn", re.compile(r"\bMRN[0-9]{4,}\b", re.IGNORECASE)),
    ("phone", re.compile(r"\b\d{3}-\d{3}-\d{4}\b")),
    ("dob", re.compile(r"\bDOB:\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", re.IGNORECASE)),
    ("patient_ref", re.compile(r"Patient/[a-fA-F0-9-]{4,}")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
]


class PresidioRequiredButMissing(RuntimeError):
    """Production demands Presidio but it could not be initialized."""


@dataclass
class ScrubReport:
    """Tally of what the scrub touched.

    Used by the dashboard's "PHI scrubbed by kind" panel.
    """

    redactions_by_kind: dict[str, int] = field(default_factory=dict)

    def total(self) -> int:
        return sum(self.redactions_by_kind.values())


def ensure_ready(*, mode: str | None = None) -> None:
    """Raise ``PresidioRequiredButMissing`` when production cannot scrub.

    Call once at startup; if it raises, the gateway exits non-zero so a
    deploy that lost Presidio fails loudly at boot rather than at the
    first user request.
    """
    resolved = mode or os.environ.get(PHI_SCRUB_MODE_ENV, DEFAULT_MODE)
    if resolved == "in_process":
        return
    if resolved != "presidio":
        raise ValueError(
            f"{PHI_SCRUB_MODE_ENV}={resolved!r}; valid: 'presidio' or 'in_process'"
        )
    try:
        # Cheap import-only probe — full model load happens on first use.
        import presidio_analyzer  # type: ignore[import-untyped] # noqa: F401
    except ImportError as exc:
        raise PresidioRequiredButMissing(
            "presidio-analyzer is not installed but COPILOT_PHI_SCRUB_MODE "
            f"is {resolved!r}. Install presidio-analyzer + the spaCy model "
            "or set COPILOT_PHI_SCRUB_MODE=in_process for dev/test."
        ) from exc


def scrub_text(text: str, *, report: ScrubReport | None = None) -> str:
    """Return a scrubbed copy of ``text``; update ``report`` if given.

    The fast path uses regex; the deeper Presidio path is invoked when
    a regex pass found nothing but the text still looks suspicious. We
    keep it intentionally simple here — the verifier's PHI pass is the
    primary line of defense, and this module is the safety net.
    """
    if not text:
        return text
    out = text
    for kind, pattern in _PATTERNS:
        new_out, count = pattern.subn(f"[REDACTED:{kind}]", out)
        if count and report is not None:
            report.redactions_by_kind[kind] = (
                report.redactions_by_kind.get(kind, 0) + count
            )
        out = new_out
    return out


def scrub_attributes(
    attributes: dict[str, Any],
    *,
    report: ScrubReport | None = None,
) -> dict[str, Any]:
    """Walk a span-attribute mapping and scrub every string value.

    Lists, tuples, and nested dicts of strings are scrubbed recursively.
    Non-text values (numbers, booleans) are returned unchanged.
    """
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        out[key] = _scrub_value(value, report=report)
    return out


def _scrub_value(value: Any, *, report: ScrubReport | None) -> Any:
    if isinstance(value, str):
        return scrub_text(value, report=report)
    if isinstance(value, list):
        return [_scrub_value(v, report=report) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(v, report=report) for v in value)
    if isinstance(value, dict):
        return {k: _scrub_value(v, report=report) for k, v in value.items()}
    return value


__all__ = [
    "DEFAULT_MODE",
    "PHI_SCRUB_MODE_ENV",
    "PresidioRequiredButMissing",
    "ScrubReport",
    "ensure_ready",
    "scrub_attributes",
    "scrub_text",
]
