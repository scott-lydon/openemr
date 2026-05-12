"""Sidecar licensing module.

Owns the license check that gates /chat and the resolution of the
license state surfaced on /diagnostic.

License rows live in the sidecar's Postgres database
(``licenses`` table, see migration
``versions/20260512_0001_add_licenses_table.py``). A row is created
on Stripe's ``customer.subscription.created`` webhook and revoked on
``customer.subscription.deleted``; ``invoice.payment_failed`` flips
the status to ``past_due`` (treated as ``expired`` for gating).

Failure mode philosophy:

- A missing license row → reject /chat with HTTP 402 Payment Required.
- An expired/revoked row → reject /chat with HTTP 402.
- Database unreachable → reject /chat with HTTP 503 (NOT 200), so a
  failed Postgres never silently gives away free service.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from fastapi import Depends, HTTPException, Request

LicenseState = Literal["ok", "missing", "expired", "revoked", "unknown"]


_logger = logging.getLogger(__name__)


def _resolve_license_key() -> str:
    """Read the configured license key.

    Reads from ``COPILOT_LICENSE_KEY`` (sidecar env) which the operator
    populates from the value shown on the module's admin page after
    Stripe Checkout completes.
    """
    return os.environ.get("COPILOT_LICENSE_KEY", "").strip()


def resolve_license_state() -> LicenseState:
    """Return the license state for the configured key.

    Used by /diagnostic. Never raises — converts every failure to
    "unknown" so the diagnostic endpoint stays responsive even when
    the database is sick.
    """
    key = _resolve_license_key()
    if not key:
        return "missing"

    try:
        from sidecar.audit.db import open_audit_connection

        with open_audit_connection() as conn:
            row = conn.execute(
                "SELECT status FROM licenses WHERE license_key = %s",
                [key],
            ).fetchone()
    except Exception as exc:
        _logger.warning("license_state_db_error: %s", exc)
        return "unknown"

    if row is None:
        return "missing"
    status = (row[0] or "").lower()
    if status == "active":
        return "ok"
    if status == "past_due":
        return "expired"
    if status in {"canceled", "revoked", "incomplete"}:
        return "revoked"
    return "unknown"


def license_check(request: Request) -> None:
    """FastAPI dependency: reject if the license is not in good standing.

    Wire onto /chat (and any other paid endpoint) like:

        @router.post("/chat", dependencies=[Depends(license_check)])
        async def chat(...):
            ...

    The dependency raises HTTPException directly so the caller does not
    need to inspect a return value.
    """
    # Allow a single explicit bypass for the open-source self-host story.
    # An operator who self-hosts the sidecar can set
    # COPILOT_LICENSE_BYPASS=true to opt out of the gate, at the cost
    # of receiving no SLA or support. This is documented in
    # OPERATOR_GUIDE.md.
    if os.environ.get("COPILOT_LICENSE_BYPASS", "").lower() == "true":
        return

    state = resolve_license_state()
    if state == "ok":
        return
    if state == "unknown":
        # Distinguish DB-unreachable from definite-not-licensed so the
        # operator can debug the database without thinking they have a
        # billing issue.
        raise HTTPException(
            status_code=503,
            detail={
                "error": "license_check_db_unreachable",
                "message": (
                    "Could not reach the sidecar licenses table; refusing /chat "
                    "rather than risk granting free access. Verify "
                    "COPILOT_DATABASE_URL is correct and the licenses table "
                    "exists. Run `alembic upgrade head` if the migration is "
                    "missing."
                ),
            },
        )
    raise HTTPException(
        status_code=402,
        detail={
            "error": f"license_{state}",
            "message": (
                "Clinical Co-Pilot license is not active. Visit "
                "https://copilot.scott-lydon.dev/billing to start a trial "
                "or update payment. Current state: " + state + "."
            ),
        },
    )


__all__ = ["LicenseState", "license_check", "resolve_license_state"]
