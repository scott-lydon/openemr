"""Layer 7 of the sanitization stack: output guard.

Scans the agent's outgoing message for:

- Sensitive content (PHI patterns the verifier missed).
- Toxic content.
- ``NoRefusal`` — model "I'm sorry I can't" boilerplate that should
  never reach a clinician (responses without verified evidence go
  through the verifier's structured refusal path instead).
- Exfiltration URLs (curl-able payloads embedded in a "source quote").

Production uses ``llm-guard``'s output scanners. The fallback (when
llm-guard is unavailable) is a small set of regex patterns. Either way
a hit replaces the offending substring with ``[REDACTED]`` and records
``sanitize.layer7.blocked=true`` on the span.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Final, Protocol


logger = logging.getLogger(__name__)


# Patterns the lightweight fallback catches. The order matters: more
# specific patterns first so the redaction tag is informative.
_FALLBACK_PATTERNS: Final[list[tuple[str, re.Pattern[str]]]] = [
    ("exfil_url", re.compile(r"\bcurl\s+https?://[^\s'\"]+", re.IGNORECASE)),
    ("script_tag", re.compile(r"<script[^>]*>", re.IGNORECASE)),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone", re.compile(r"\b\d{3}-\d{3}-\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("apology_boilerplate", re.compile(r"\bI'?m sorry,? but I (cannot|can't)\b", re.IGNORECASE)),
]


@dataclass(frozen=True)
class OutputScanResult:
    """The Layer 7 output guard's verdict."""

    blocked: bool
    reasons: list[str]
    sanitized_text: str


class OutputScanner(Protocol):
    def scan(self, text: str) -> OutputScanResult:
        ...


@dataclass
class FallbackOutputScanner:
    """Pure-Python regex scanner. Used when llm-guard is unavailable
    and as the unit-test substitute."""

    patterns: list[tuple[str, re.Pattern[str]]] = field(
        default_factory=lambda: list(_FALLBACK_PATTERNS)
    )

    def scan(self, text: str) -> OutputScanResult:
        if not text:
            return OutputScanResult(blocked=False, reasons=[], sanitized_text=text)

        out = text
        reasons: list[str] = []
        for name, pattern in self.patterns:
            new_out, count = pattern.subn(f"[REDACTED:{name}]", out)
            if count > 0:
                reasons.append(name)
                out = new_out
        return OutputScanResult(
            blocked=bool(reasons),
            reasons=reasons,
            sanitized_text=out,
        )


@dataclass
class LLMGuardOutputScanner:
    """Production wrapper around llm-guard output scanners.

    Falls through to ``FallbackOutputScanner`` when llm-guard is not
    available so the module loads on every host.
    """

    fallback: FallbackOutputScanner = field(default_factory=FallbackOutputScanner)

    def scan(self, text: str) -> OutputScanResult:
        try:
            from llm_guard import scan_output  # type: ignore[import-untyped]
            from llm_guard.output_scanners import (  # type: ignore[import-untyped]
                NoRefusal, Sensitive, Toxicity,
            )
        except ImportError:
            return self.fallback.scan(text)

        try:
            scanners = [Sensitive(), Toxicity(), NoRefusal()]
            sanitized, results, scores = scan_output(scanners, "", text)
        except Exception as exc:
            logger.warning(
                "llm-guard output scan failed (%s); falling back",
                exc,
            )
            return self.fallback.scan(text)

        reasons: list[str] = []
        for scanner_name, valid in results.items():
            if not valid:
                reasons.append(scanner_name.lower())
        return OutputScanResult(
            blocked=bool(reasons),
            reasons=reasons,
            sanitized_text=str(sanitized) if sanitized else text,
        )


__all__ = [
    "FallbackOutputScanner",
    "LLMGuardOutputScanner",
    "OutputScanResult",
    "OutputScanner",
]
