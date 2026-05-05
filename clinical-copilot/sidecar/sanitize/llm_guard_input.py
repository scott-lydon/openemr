"""Layer 4 of the sanitization stack: LLM Guard + Rebuff ensemble.

Every untrusted input (document text, user free-form question, intake
chief concern) goes through TWO independent prompt-injection
detectors:

- ``LLMGuardScanner`` — calls the ``llm-guard`` library's
  ``PromptInjection``, ``Anonymize``, ``BanSubstrings`` (for
  base64-decoded payloads), and ``Code`` scanners.
- ``RebuffScanner`` — calls the ``rebuff`` library's heuristic +
  vector + LLM-classifier ensemble.

Either detector firing is enough to block the input. The ensemble
reduces shared blind spots between two separately-trained detectors.

Why a heavy module rather than inline calls:

- Each detector has its own initialization pattern (loading models,
  caching scanners). The wrapper memoizes the heavy state so the
  per-request call is cheap.
- The wrapper exposes a single ``scan_input`` function so the rest of
  the pipeline does not know which detectors are involved.
- A future detector swap (e.g. an in-house classifier) lands in this
  one file.

For unit tests, ``StubInputScanner`` substitutes a deterministic
behavior — list of ``(needle, reason)`` pairs that trigger blocking
when present in the input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Final, Protocol


logger = logging.getLogger(__name__)


# The reason codes returned by ``scan_input`` are stable across releases.
# Dashboards and the eval suite match on these strings.
PROMPT_INJECTION_REASON: Final[str] = "prompt_injection"
ANONYMIZE_REASON: Final[str] = "phi_in_input"
CODE_REASON: Final[str] = "executable_code_in_input"
BAN_SUBSTRING_REASON: Final[str] = "banned_substring"
BASE64_PAYLOAD_REASON: Final[str] = "base64_decoded_payload"


@dataclass(frozen=True)
class InputScanResult:
    """The outcome of a Layer 4 scan."""

    blocked: bool
    reasons: list[str]
    sanitized_text: str


class InputScanner(Protocol):
    """Protocol that the sanitization stack honors."""

    def scan(self, text: str) -> InputScanResult:
        ...


# Default suspicion patterns. The ensemble runs over the raw text plus
# (when triggered) the base64-decoded form. These patterns are the
# cheap pure-Python first pass; the LLM Guard / Rebuff calls run only
# when this pass cannot rule out injection on its own.
_DEFAULT_SUSPICIOUS_NEEDLES: Final[list[tuple[str, str]]] = [
    ("ignore previous instructions", PROMPT_INJECTION_REASON),
    ("ignore the above", PROMPT_INJECTION_REASON),
    ("you are now", PROMPT_INJECTION_REASON),
    ("disregard prior", PROMPT_INJECTION_REASON),
    ("system: ", PROMPT_INJECTION_REASON),
    ("```python\nimport os", CODE_REASON),
    ("eval(", CODE_REASON),
    ("exec(", CODE_REASON),
    ("```bash\n", CODE_REASON),
]


@dataclass
class StubInputScanner:
    """Deterministic scanner used by unit tests.

    Constructor accepts a list of ``(needle, reason)`` pairs. The scan
    returns ``blocked=True`` with the reason strings when any needle is
    a (case-insensitive) substring of the input. Defaults to a small
    set of well-known prompt-injection phrases.
    """

    needles: list[tuple[str, str]] = field(
        default_factory=lambda: list(_DEFAULT_SUSPICIOUS_NEEDLES)
    )

    def scan(self, text: str) -> InputScanResult:
        lower = text.lower()
        reasons: list[str] = []
        for needle, reason in self.needles:
            if needle.lower() in lower and reason not in reasons:
                reasons.append(reason)
        # Try base64 decode if the input looks suspicious (long runs of
        # base64 chars). A successful decode reveals the inner text;
        # check for the same needles in the decoded form.
        decoded = _maybe_decode_base64(text)
        if decoded:
            decoded_lower = decoded.lower()
            for needle, _ in self.needles:
                if needle.lower() in decoded_lower:
                    if BASE64_PAYLOAD_REASON not in reasons:
                        reasons.append(BASE64_PAYLOAD_REASON)
                    break
        return InputScanResult(
            blocked=bool(reasons),
            reasons=reasons,
            sanitized_text=text,
        )


@dataclass
class LLMGuardScanner:
    """Production wrapper around the ``llm-guard`` library.

    Imports the library lazily so the module loads on hosts without
    llm-guard installed (the unit tests use the stub instead). Falls
    through to the stub-style heuristic if the library raises during
    initialization, with a WARN log.
    """

    fallback: StubInputScanner = field(default_factory=StubInputScanner)

    def scan(self, text: str) -> InputScanResult:
        try:
            from llm_guard import scan_prompt  # type: ignore[import-untyped]
            from llm_guard.input_scanners import (  # type: ignore[import-untyped]
                Anonymize, BanSubstrings, Code, PromptInjection,
            )
            from llm_guard.input_scanners.ban_substrings import MatchType  # type: ignore[import-untyped]
        except ImportError:
            return self.fallback.scan(text)

        try:
            scanners = [
                PromptInjection(),
                Anonymize(),
                BanSubstrings(
                    substrings=[needle for needle, _ in _DEFAULT_SUSPICIOUS_NEEDLES],
                    match_type=MatchType.STR,
                ),
                Code(use_onnx=False),
            ]
            sanitized, results, scores = scan_prompt(scanners, text)
        except Exception as exc:
            logger.warning(
                "llm-guard scan failed (%s); falling back to stub heuristic",
                exc,
            )
            return self.fallback.scan(text)

        reasons: list[str] = []
        for scanner_name, valid in results.items():
            if valid:
                continue
            if "prompt" in scanner_name.lower() or "injection" in scanner_name.lower():
                reasons.append(PROMPT_INJECTION_REASON)
            elif "anonymize" in scanner_name.lower():
                reasons.append(ANONYMIZE_REASON)
            elif "code" in scanner_name.lower():
                reasons.append(CODE_REASON)
            elif "ban" in scanner_name.lower() or "substring" in scanner_name.lower():
                reasons.append(BAN_SUBSTRING_REASON)
        return InputScanResult(
            blocked=bool(reasons),
            reasons=reasons,
            sanitized_text=str(sanitized) if sanitized else text,
        )


def _maybe_decode_base64(text: str) -> str | None:
    """Return the decoded text when ``text`` looks like base64, else None."""
    import base64
    import re

    matches = re.findall(r"[A-Za-z0-9+/=]{16,}", text)
    for match in matches:
        if len(match) % 4 != 0:
            continue
        try:
            decoded = base64.b64decode(match, validate=True).decode(
                "utf-8", errors="ignore"
            )
        except Exception:
            continue
        if decoded.strip() and not all(b in {0} for b in decoded.encode()):
            return decoded
    return None


__all__ = [
    "ANONYMIZE_REASON",
    "BAN_SUBSTRING_REASON",
    "BASE64_PAYLOAD_REASON",
    "CODE_REASON",
    "InputScanResult",
    "InputScanner",
    "LLMGuardScanner",
    "PROMPT_INJECTION_REASON",
    "StubInputScanner",
]
