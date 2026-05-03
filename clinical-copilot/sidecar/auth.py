"""Sidecar JWT auth.

The sidecar accepts BFF-minted task tokens (HS256 JWTs). The same
``mint_task_token`` / ``verify_task_token`` primitives are also re-exported
by ``bff/oauth.py`` so the BFF can sign tokens; keeping the canonical
implementation in the sidecar inverts the historical bff→sidecar import
direction and lets the sidecar enforce auth without a cross-package import.

Token shape (HS256):

    header  = {"alg":"HS256","typ":"JWT"}
    payload = {
        "iss": "clinical-copilot-bff" | "openemr-launch",
        "sub": <user_id>,                # OpenEMR username
        "patient_id": "Patient/<uuid>",  # FHIR resource id
        "purpose_of_use": ["diagnostic_cross_check", "chart_error_scan",
                           "follow_up_question"],   # JSON array
        "scope": "<space-separated SMART scopes>",
        "iat": <unix>,
        "nbf": <unix>,
        "exp": <unix>,                   # 5 minutes after iat
        "jti": <random>,
    }

The ``purpose_of_use`` claim is a JSON array of every purpose the holder
is authorised to invoke during the token's lifetime. The UI fans out
multiple ``/chat`` calls (one per purpose) from a single launch click;
binding the token to only one purpose would force the UI to round-trip
to the launch endpoint per purpose. The audit log still records the
per-call purpose, so authorisation breadth and exercised purpose remain
distinguishable.

Backward-compatible: a token whose ``purpose_of_use`` is a plain string
(legacy minter) is treated as a single-element list. New tokens always
emit an array. This forward-compat path lets in-flight tokens (the
5-minute lifetime) survive a deploy that updates the minter and the
verifier together.

Verification failures throw ``TaskTokenError`` subclasses (each one names a
single, unambiguous failure mode) so callers can return precise 401 reasons
to the client without leaking implementation details.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException, status

from sidecar.config import get_settings


# ─── Errors ─────────────────────────────────────────────────────────────────


class TaskTokenError(ValueError):
    """Base class for any task-token verification failure."""


class TaskTokenMalformedError(TaskTokenError):
    """Token is not three dot-separated base64url segments."""


class TaskTokenSignatureError(TaskTokenError):
    """HMAC signature does not match the configured signing key."""


class TaskTokenExpiredError(TaskTokenError):
    """``exp`` claim is in the past."""


class TaskTokenNotYetValidError(TaskTokenError):
    """``nbf`` claim is in the future (clock skew or replay)."""


class TaskTokenMissingClaimError(TaskTokenError):
    """A required claim (``sub``, ``patient_id``, ``purpose_of_use``) is missing."""


class AuthorizationHeaderError(TaskTokenError):
    """The HTTP ``Authorization`` header is missing or malformed."""


# ─── base64url helpers ──────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    """Encode bytes as base64url without ``=`` padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """Decode base64url, restoring ``=`` padding if needed."""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


# ─── Mint / verify ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskTokenClaims:
    """Verified, downscoped task-token claims.

    ``authorized_purposes`` is the JSON array decoded from the
    ``purpose_of_use`` claim — every purpose the holder may invoke
    during the token's lifetime. Always non-empty; verifier rejects
    tokens with an empty array.
    """

    user_id: str
    patient_id: str
    authorized_purposes: tuple[str, ...]
    scope: str
    issuer: str
    expires_at: int

    def is_purpose_authorized(self, purpose: str) -> bool:
        """Return True if ``purpose`` is in the authorised set."""
        return purpose in self.authorized_purposes


def mint_task_token(
    *,
    signing_key: str,
    user_id: str,
    patient_id: str,
    purposes_of_use: Sequence[str],
    scopes: list[str],
    lifetime_seconds: int = 300,
    issuer: str = "clinical-copilot-bff",
) -> str:
    """Mint an HS256 JWT representing one downscoped task.

    ``purposes_of_use`` is the list of purpose codes the holder may
    invoke during the token's lifetime (e.g.
    ``("diagnostic_cross_check", "chart_error_scan")``). Each ``/chat``
    call still passes a single purpose; the sidecar verifies membership.
    Audit rows record the per-call purpose, so the breadth of
    authorisation and the actually-exercised purpose remain
    distinguishable.

    Constraints:

    - ``purposes_of_use`` must be non-empty; minting a token with no
      authorised purposes would render it unusable for ``/chat`` calls,
      which is always an upstream bug.
    - Each entry must be a non-empty string. Mixed types or empty
      strings are rejected upfront so the failure surfaces at mint time
      rather than at verify time.

    The sidecar verifies this token with the same signing key. Real
    deployments swap to RS256 with rotating keys; HS256 with a 32-byte
    key is acceptable for an internal trust boundary that is also fronted
    by mTLS / nginx + private network.
    """
    if not signing_key or signing_key == "change-me-to-a-32-byte-hex-string":
        raise ValueError(
            "refusing to mint token with default signing key; "
            "set COPILOT_BFF_JWT_SIGNING_KEY to a 32-byte random secret"
        )
    purposes_list = list(purposes_of_use)
    if not purposes_list:
        raise ValueError(
            "purposes_of_use must contain at least one purpose code; "
            "minting a token with zero authorised purposes is always a bug"
        )
    for index, purpose in enumerate(purposes_list):
        if not isinstance(purpose, str):
            raise ValueError(
                f"purposes_of_use[{index}] must be a string, got "
                f"{type(purpose).__name__}: {purpose!r}"
            )
        if not purpose:
            raise ValueError(
                f"purposes_of_use[{index}] is empty; "
                "every entry must be a non-empty purpose code"
            )
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "sub": user_id,
        "patient_id": patient_id,
        "purpose_of_use": purposes_list,
        "scope": " ".join(scopes),
        "iat": now,
        "nbf": now,
        "exp": now + lifetime_seconds,
        "jti": secrets.token_urlsafe(8),
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = hmac.new(signing_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{_b64url(sig)}"


def verify_task_token(token: str, *, signing_key: str) -> TaskTokenClaims:
    """Verify and decode a task token.

    Raises a specific :class:`TaskTokenError` subclass for each failure mode
    so the caller can return a precise 401 reason without leaking secrets.
    """
    if not signing_key or signing_key == "change-me-to-a-32-byte-hex-string":
        raise TaskTokenSignatureError(
            "sidecar refuses to verify with the default signing key; "
            "set COPILOT_BFF_JWT_SIGNING_KEY"
        )
    parts = token.split(".")
    if len(parts) != 3:
        raise TaskTokenMalformedError(
            f"expected 3 dot-separated segments, got {len(parts)}"
        )
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    expected = hmac.new(signing_key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    try:
        actual = _b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise TaskTokenMalformedError(f"signature segment is not base64url: {exc}") from exc
    if not hmac.compare_digest(expected, actual):
        raise TaskTokenSignatureError("signature does not match signing key")
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:  # noqa: BLE001
        raise TaskTokenMalformedError(f"payload is not valid JSON: {exc}") from exc
    now = int(time.time())
    exp = int(payload.get("exp", 0))
    nbf = int(payload.get("nbf", 0))
    if exp <= now:
        raise TaskTokenExpiredError(
            f"token expired at {exp}, now is {now} ({now - exp}s ago)"
        )
    if nbf > now:
        raise TaskTokenNotYetValidError(
            f"token not valid until {nbf}, now is {now} ({nbf - now}s in the future)"
        )
    for required in ("sub", "patient_id", "purpose_of_use"):
        if not payload.get(required):
            raise TaskTokenMissingClaimError(f"missing required claim: {required!r}")
    raw_purpose: Any = payload["purpose_of_use"]
    if isinstance(raw_purpose, str):
        # Backward-compatible: legacy minter emitted a single string.
        # Treat as a one-element list so in-flight tokens survive a
        # deploy that rolls the minter and verifier together.
        authorized_purposes: tuple[str, ...] = (raw_purpose,)
    elif isinstance(raw_purpose, list):
        if not raw_purpose:
            raise TaskTokenMissingClaimError(
                "purpose_of_use claim is an empty array; "
                "a token with no authorised purposes cannot be used"
            )
        for index, value in enumerate(raw_purpose):
            if not isinstance(value, str) or not value:
                raise TaskTokenMissingClaimError(
                    f"purpose_of_use[{index}] is not a non-empty string: "
                    f"{value!r}"
                )
        authorized_purposes = tuple(raw_purpose)
    else:
        raise TaskTokenMissingClaimError(
            f"purpose_of_use must be a string or array of strings, got "
            f"{type(raw_purpose).__name__}"
        )
    return TaskTokenClaims(
        user_id=str(payload["sub"]),
        patient_id=str(payload["patient_id"]),
        authorized_purposes=authorized_purposes,
        scope=str(payload.get("scope", "")),
        issuer=str(payload.get("iss", "")),
        expires_at=exp,
    )


# ─── FastAPI dependency ─────────────────────────────────────────────────────


def require_task_token(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> TaskTokenClaims:
    """FastAPI dependency: 401 unless the request carries a valid task token.

    Bind to a route with ``Depends(require_task_token)``. The claims are
    returned to the handler so it can enforce per-patient binding (the
    token's ``patient_id`` must match the requested patient).
    """
    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_authorization_header",
                "message": "Authorization header required (Bearer <jwt>)",
            },
            headers={"WWW-Authenticate": 'Bearer realm="clinical-copilot-sidecar"'},
        )
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "malformed_authorization_header",
                "message": "expected 'Bearer <jwt>'",
            },
            headers={"WWW-Authenticate": 'Bearer realm="clinical-copilot-sidecar"'},
        )
    token = parts[1].strip()
    settings = get_settings()
    try:
        return verify_task_token(token, signing_key=settings.bff_jwt_signing_key)
    except TaskTokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "token_expired", "message": str(exc)},
            headers={"WWW-Authenticate": 'Bearer realm="clinical-copilot-sidecar", error="invalid_token"'},
        ) from exc
    except TaskTokenSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "bad_signature", "message": str(exc)},
            headers={"WWW-Authenticate": 'Bearer realm="clinical-copilot-sidecar", error="invalid_token"'},
        ) from exc
    except TaskTokenMissingClaimError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_claim", "message": str(exc)},
        ) from exc
    except TaskTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": str(exc)},
            headers={"WWW-Authenticate": 'Bearer realm="clinical-copilot-sidecar", error="invalid_token"'},
        ) from exc


__all__ = [
    "AuthorizationHeaderError",
    "TaskTokenClaims",
    "TaskTokenError",
    "TaskTokenExpiredError",
    "TaskTokenMalformedError",
    "TaskTokenMissingClaimError",
    "TaskTokenNotYetValidError",
    "TaskTokenSignatureError",
    "mint_task_token",
    "require_task_token",
    "verify_task_token",
]
