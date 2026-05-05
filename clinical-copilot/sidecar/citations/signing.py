"""Time-limited signed Uniform Resource Locators (URLs) for citation
preview endpoints.

The flow:

1. The chat layer mints a signed URL by calling ``mint_signed_url``
   when it embeds a citation chip in a response. The URL carries the
   citation id, the expiry timestamp, and an HMAC-SHA256 signature.
2. The clinician's browser fetches the URL when they click the chip.
3. The preview endpoint verifies the signature, checks the expiry,
   and serves the rendered Portable Network Graphics (PNG) — but only
   after re-checking that the caller's task token still has scope on
   the citation's patient.

Why a signed URL rather than just bearer-token-protected:

- The URL is rendered into the chat HTML as an ``<img src=...>``;
  the browser cannot send the bearer token automatically. A short-lived
  signed URL is the standard pattern for image fetches.
- The expiry binds the URL to the visible chat session; after the user
  closes the chat the URL stops working.

Time To Live (TTL):

- Default 5 minutes. Short enough that a leaked URL is not a long-term
  liability; long enough that a clinician can read the chat and click
  through without expiry mid-session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Final


SIGNED_URL_DEFAULT_TTL_SECONDS: Final[int] = 300
SIGNATURE_VERSION: Final[str] = "v1"


class SignedUrlError(Exception):
    """Base for signed-URL verification failures."""


class SignedUrlExpired(SignedUrlError):
    """The URL is past its expiry."""


class SignedUrlSignatureInvalid(SignedUrlError):
    """The signature does not match (tampering or wrong key)."""


@dataclass(frozen=True)
class SignedToken:
    """The decoded payload of a signed URL.

    ``citation_id`` identifies the citation row to render. ``patient_id``
    is recorded so the preview endpoint can scope-check the caller's
    task token without a second database lookup. ``expires_at`` is a
    Unix timestamp in seconds.
    """

    version: str
    citation_id: str
    patient_id: str
    expires_at: int


def mint_signed_url(
    *,
    base_url: str,
    citation_id: str,
    patient_id: str,
    signing_key: str,
    ttl_seconds: int = SIGNED_URL_DEFAULT_TTL_SECONDS,
    now_unix: int | None = None,
) -> str:
    """Build a signed URL.

    ``base_url`` is everything before the query string, e.g.
    ``"https://api.example.com/agent-api/v1/citations/{cid}/preview.png"``.
    The function appends a ``?token=...`` query parameter.

    Raises ``ValueError`` for misconfigured inputs (empty key, non-positive
    TTL) so a deploy without secrets fails loudly at the first call,
    not at a leak event months later.
    """
    if not signing_key or len(signing_key) < 16:
        raise ValueError(
            "signing_key must be at least 16 characters; the BFF JWT "
            "signing key meets this. Refusing to mint a weakly-signed URL."
        )
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")

    expires_at = (now_unix if now_unix is not None else int(time.time())) + ttl_seconds
    payload = {
        "v": SIGNATURE_VERSION,
        "cid": citation_id,
        "pid": patient_id,
        "exp": expires_at,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
    sig = hmac.new(signing_key.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    token = f"{payload_b64}.{sig_b64}"

    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}token={token}"


def verify_signed_url(
    *,
    token: str,
    signing_key: str,
    now_unix: int | None = None,
) -> SignedToken:
    """Decode and verify a signed URL token.

    Raises ``SignedUrlSignatureInvalid`` for any tampering or
    malformedness, ``SignedUrlExpired`` when the URL is past its
    expiry. Both subclass ``SignedUrlError``.
    """
    if not token:
        raise SignedUrlSignatureInvalid("empty token")

    parts = token.split(".")
    if len(parts) != 2:
        raise SignedUrlSignatureInvalid(
            "token must have the shape '<payload>.<signature>'"
        )
    payload_b64, sig_b64 = parts

    expected_sig = hmac.new(
        signing_key.encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(expected_sig_b64, sig_b64):
        raise SignedUrlSignatureInvalid("signature mismatch")

    try:
        padding = "=" * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, TypeError) as exc:
        raise SignedUrlSignatureInvalid(
            f"payload decode failed: {type(exc).__name__}: {exc!s}"
        ) from exc

    for required in ("v", "cid", "pid", "exp"):
        if required not in payload:
            raise SignedUrlSignatureInvalid(
                f"payload missing required field {required!r}"
            )
    if payload["v"] != SIGNATURE_VERSION:
        raise SignedUrlSignatureInvalid(
            f"signature version {payload['v']!r}; expected {SIGNATURE_VERSION!r}"
        )

    now = now_unix if now_unix is not None else int(time.time())
    if int(payload["exp"]) <= now:
        raise SignedUrlExpired(
            f"signed URL expired at {payload['exp']} (now {now})"
        )

    return SignedToken(
        version=payload["v"],
        citation_id=str(payload["cid"]),
        patient_id=str(payload["pid"]),
        expires_at=int(payload["exp"]),
    )


__all__ = [
    "SIGNED_URL_DEFAULT_TTL_SECONDS",
    "SIGNATURE_VERSION",
    "SignedToken",
    "SignedUrlError",
    "SignedUrlExpired",
    "SignedUrlSignatureInvalid",
    "mint_signed_url",
    "verify_signed_url",
]
