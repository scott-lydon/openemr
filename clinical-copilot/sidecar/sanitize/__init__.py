"""Seven-layer input + output sanitization stack.

Layer mapping (matches ``W2_QUALITY_PLAN.md`` §8):

- Layer 1 — MIME whitelist + ClamAV (``sidecar.ingest.mime`` and
  ``sidecar.ingest.virus_scan``).
- Layer 2 — Spotlighting envelopes (``sidecar.sanitize.spotlighting``).
- Layer 3 — Strict structured output (Pydantic schemas in
  ``sidecar.schemas.w2``).
- Layer 4 — LLM Guard + Rebuff input ensemble
  (``sidecar.sanitize.llm_guard_input``).
- Layer 5 — Presidio Personal Health Information (PHI) scrubber
  (``sidecar.agents.w2.verifier``).
- Layer 6 — Tool-call router (``sidecar.sanitize.tool_router``).
- Layer 7 — Output guard (``sidecar.sanitize.llm_guard_output``).

Public surface re-exported here so callers can write
``from sidecar.sanitize import make_envelope`` without reaching deep
into module paths.
"""

from sidecar.sanitize.llm_guard_input import (
    ANONYMIZE_REASON,
    BAN_SUBSTRING_REASON,
    BASE64_PAYLOAD_REASON,
    CODE_REASON,
    InputScanResult,
    InputScanner,
    LLMGuardScanner,
    PROMPT_INJECTION_REASON,
    StubInputScanner,
)
from sidecar.sanitize.llm_guard_output import (
    FallbackOutputScanner,
    LLMGuardOutputScanner,
    OutputScanResult,
    OutputScanner,
)
from sidecar.sanitize.spotlighting import (
    SENTINEL_BYTES,
    SpotlightEnvelope,
    make_envelope,
    response_echoes_sentinel,
)
from sidecar.sanitize.tool_router import (
    DeterministicToolRouter,
    TOOL_ROUTER_PROMPT_VERSION,
    ToolCallRequest,
    ToolName,
    ToolRouter,
    ToolRouterDecision,
)

__all__ = [
    "ANONYMIZE_REASON",
    "BAN_SUBSTRING_REASON",
    "BASE64_PAYLOAD_REASON",
    "CODE_REASON",
    "DeterministicToolRouter",
    "FallbackOutputScanner",
    "InputScanResult",
    "InputScanner",
    "LLMGuardOutputScanner",
    "LLMGuardScanner",
    "OutputScanResult",
    "OutputScanner",
    "PROMPT_INJECTION_REASON",
    "SENTINEL_BYTES",
    "SpotlightEnvelope",
    "StubInputScanner",
    "TOOL_ROUTER_PROMPT_VERSION",
    "ToolCallRequest",
    "ToolName",
    "ToolRouter",
    "ToolRouterDecision",
    "make_envelope",
    "response_echoes_sentinel",
]
