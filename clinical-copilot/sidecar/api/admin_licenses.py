"""POST /admin/licenses — issue a license key over HTTP.

Authentication
==============

Bearer-token only, compared in constant time against the
``COPILOT_ADMIN_TOKEN`` environment variable. The endpoint refuses
every request when the env var is unset, so a forgotten config does
NOT silently expose key issuance to the world.

Why not session/cookie auth?

The admin endpoint is meant to be hit from a curl one-liner or a small
internal admin UI, not from a browser session. Bearer tokens are the
simplest thing that works for both.

Token rotation
==============

To rotate ``COPILOT_ADMIN_TOKEN`` without downtime: set the new value,
restart the sidecar, then invalidate old uses by simply not using them.
There is no list of "valid tokens" — the env var is the single source
of truth.

Error handling
==============

The endpoint surfaces ``GenerateLicenseError.message`` verbatim in the
400 response body so the operator sees the same diagnostic the CLI
prints. We deliberately do NOT swallow the message into a generic
"bad request"; the message names the fix.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from sidecar.licensing.generate import (
    GenerateLicenseError,
    GenerateLicenseInput,
    generate_license_key,
)

router = APIRouter()
_logger = logging.getLogger(__name__)


class IssueLicenseRequest(BaseModel):
    """Body of POST /admin/licenses.

    Field names mirror the CLI flags on the generator module so an
    operator who has used one can use the other.
    """

    plan: str = Field(
        ...,
        description=(
            "Tier the key entitles. One of: starter, pro, enterprise. "
            "Validated by the generator."
        ),
    )
    customer_email: Optional[str] = Field(
        default=None,
        description=(
            "Echoed back in the response so an admin UI can paste it "
            "into the welcome email. NOT stored on the licenses row."
        ),
    )
    stripe_customer_id: Optional[str] = Field(
        default=None,
        description="Stripe customer ID. Optional for non-Stripe trials.",
    )
    seats: int = Field(default=1, ge=1)
    trial_days: Optional[int] = Field(
        default=None,
        ge=0,
        description="If set, the key is a trial that expires after N days.",
    )
    status: str = Field(
        default="active",
        description=(
            "Initial row status. One of: active, past_due, canceled, "
            "revoked, incomplete. Default 'active'."
        ),
    )


class IssueLicenseResponse(BaseModel):
    """Response body of POST /admin/licenses."""

    license_key: str
    plan: str
    status: str
    seats: int
    trial_ends_at: Optional[str] = None
    customer_email: Optional[str] = None


def _check_admin_token(authorization: Optional[str]) -> None:
    """Reject the request unless a valid bearer token is present.

    Raises HTTPException with a precise message on every failure case so
    the operator can diagnose without reading sidecar logs.
    """
    expected = os.environ.get("COPILOT_ADMIN_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "admin_disabled",
                "message": (
                    "COPILOT_ADMIN_TOKEN is not set on the sidecar; admin "
                    "endpoints refuse every request when unset. Set the "
                    "env var to a long random secret (e.g. "
                    "`python -c 'import secrets; print(secrets.token_urlsafe(32))'`) "
                    "and restart the sidecar."
                ),
            },
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_bearer",
                "message": (
                    "Authorization: Bearer <token> header is required. "
                    "Curl example: -H \"Authorization: Bearer $COPILOT_ADMIN_TOKEN\""
                ),
            },
        )
    presented = authorization[len("Bearer ") :].strip()
    # secrets.compare_digest is constant-time so a brute-force token-
    # guesser cannot use response-time variance as a side channel.
    if not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "bad_admin_token",
                "message": (
                    "Admin token did not match. Verify the curl call uses "
                    "the same COPILOT_ADMIN_TOKEN value as the running "
                    "sidecar."
                ),
            },
        )


@router.post(
    "/admin/licenses",
    response_model=IssueLicenseResponse,
    tags=["admin"],
    summary="Issue a new license key",
)
def issue_license(
    req: IssueLicenseRequest,
    authorization: Optional[str] = Header(default=None),
) -> IssueLicenseResponse:
    """Issue a new license key. Admin-only.

    Curl example::

        curl -X POST https://<sidecar>/admin/licenses \\
            -H "Authorization: Bearer $COPILOT_ADMIN_TOKEN" \\
            -H "Content-Type: application/json" \\
            -d '{"plan": "pro", "trial_days": 14, "customer_email": "user@example.com"}'
    """
    _check_admin_token(authorization)

    try:
        out = generate_license_key(
            GenerateLicenseInput(
                plan=req.plan,  # type: ignore[arg-type]
                customer_email=req.customer_email,
                stripe_customer_id=req.stripe_customer_id,
                seats=req.seats,
                trial_days=req.trial_days,
                status=req.status,  # type: ignore[arg-type]
            ),
        )
    except GenerateLicenseError as exc:
        # Surface the generator's operator-ready message verbatim. The
        # generator already includes "common causes" remediation text.
        raise HTTPException(
            status_code=400,
            detail={"error": "generate_failed", "message": str(exc)},
        ) from exc

    return IssueLicenseResponse(
        license_key=out.license_key,
        plan=out.plan,
        status=out.status,
        seats=out.seats,
        trial_ends_at=out.trial_ends_at.isoformat() if out.trial_ends_at else None,
        customer_email=out.customer_email,
    )
