"""Layer 2 of the seven-layer sanitization stack: spotlighting envelopes.

When a planner Large Language Model (LLM) sees document content
(extracted intake form, fax body, lab notes), the document is wrapped
in a "spotlight" envelope: a unique random sentinel string at the top
and bottom plus per-line sentinel prefixes. The LLM's prompt instructs
it to treat any text inside the envelope as data, never as instructions.

The envelope is unforgeable in practice: an attacker who somehow knows
the structure cannot match the per-document random sentinel without
having seen this specific document's envelope.

The pattern is documented in Microsoft Research's spotlighting paper
(arxiv.org/abs/2403.14720). Our implementation produces a token that
is:

- Cryptographically random (32 bytes from ``os.urandom``).
- URL-safe base64 (no characters that confuse markdown or JSON).
- Per-document — one envelope per call, never reused.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Final


# 32 bytes -> 256 bits of entropy. URL-safe base64 of 32 bytes is 43
# characters, more than enough that a random text sequence will never
# collide.
SENTINEL_BYTES: Final[int] = 32


@dataclass(frozen=True)
class SpotlightEnvelope:
    """A wrapped document plus its sentinel for downstream verification.

    The verifier (Phase 5) re-checks that the sentinel did not leak into
    the model's response. A response that echoes the sentinel triggers a
    high-severity span event and a refusal, because echoing the sentinel
    means the model treated the envelope as instructions to repeat back.
    """

    sentinel: str
    wrapped_text: str


def make_envelope(text: str) -> SpotlightEnvelope:
    """Wrap ``text`` in a spotlighting envelope.

    The wrapped text format is::

        <sentinel>BEGIN
        <sentinel>:: line one
        <sentinel>:: line two
        ...
        <sentinel>END

    Each line carries the sentinel prefix so an attacker cannot inject a
    fake terminator and break out of the envelope.
    """
    sentinel = _new_sentinel()
    if not text:
        wrapped = f"{sentinel}BEGIN\n{sentinel}::\n{sentinel}END\n"
    else:
        prefixed_lines = "\n".join(f"{sentinel}:: {line}" for line in text.splitlines())
        wrapped = f"{sentinel}BEGIN\n{prefixed_lines}\n{sentinel}END\n"
    return SpotlightEnvelope(sentinel=sentinel, wrapped_text=wrapped)


def response_echoes_sentinel(envelope: SpotlightEnvelope, response_text: str) -> bool:
    """Return True when ``response_text`` contains the envelope's sentinel.

    A True response means the model leaked the sentinel — almost
    certainly because instructions inside the envelope succeeded in
    making the model echo it back. The verifier should refuse.
    """
    return envelope.sentinel in response_text


def _new_sentinel() -> str:
    raw = os.urandom(SENTINEL_BYTES)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


__all__ = [
    "SENTINEL_BYTES",
    "SpotlightEnvelope",
    "make_envelope",
    "response_echoes_sentinel",
]
