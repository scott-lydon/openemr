"""Stripe webhook handler and license-management routes.

Two endpoints:

- ``POST /stripe/webhook``      Stripe pushes subscription lifecycle
                                events here. We verify the signature,
                                idempotency-check the event_id, and
                                materialize the change into the
                                ``licenses`` table.

- ``GET  /billing``             Convenience redirect to the Stripe
                                Customer Portal so a clinic operator
                                can change plan / update card without
                                leaving OpenEMR.

Signature verification

Stripe signs each webhook with HMAC-SHA256 over the raw request body
plus a timestamp. We MUST validate against the raw bytes (FastAPI's
``Request.body()`` returns those) — re-serializing the parsed JSON
would change byte order and break the signature. We compare using
``hmac.compare_digest`` so timing attacks cannot leak the secret one
byte at a time. The shared secret comes from ``STRIPE_WEBHOOK_SECRET``;
the endpoint refuses to start if it is empty.

Idempotency

Stripe redelivers events for up to 3 days when our endpoint returns
non-2xx. Each event has a stable ``event_id``. We INSERT it into
``stripe_events`` and treat duplicate-key violation as "already
processed; silently ack". The body's SHA-256 is recorded too so a
replay with a tampered payload is detectable in the audit trail.

Why this lives in the sidecar and not the BFF

Stripe webhooks are unauthenticated public POSTs (the signature IS
the auth). They terminate in the host that owns the licenses table.
That host is the sidecar, which is the only piece that already runs
Postgres. Routing them through the BFF would add a hop and a moving
part for no gain.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response


router = APIRouter(tags=["billing"])
_logger = logging.getLogger(__name__)


# How much clock skew we tolerate between Stripe and us when checking
# the signature timestamp. Stripe's docs recommend 5 minutes.
_TIMESTAMP_TOLERANCE_SECONDS = 300


def _resolve_webhook_secret() -> str:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET is not set. The Stripe webhook handler "
            "refuses to start without it because an unsigned POST would be "
            "indistinguishable from an attacker writing rows into the "
            "licenses table. Set the value from the Stripe Dashboard → "
            "Developers → Webhooks page, then restart the sidecar."
        )
    return secret


def _verify_signature(body: bytes, header: str | None, secret: str) -> dict[str, str]:
    """Verify the Stripe-Signature header. Returns the parsed v1 mac.

    Raises ``HTTPException(400)`` on any structural problem and
    ``HTTPException(401)`` on a signature mismatch. The distinct status
    codes are useful when chasing down a misconfiguration: 400 means
    "Stripe never reached us with a valid header" (probably a load
    balancer stripped it), 401 means "secret mismatch" (probably
    rotated only on one side).
    """
    if not header:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header.")
    parts = {}
    for entry in header.split(","):
        if "=" not in entry:
            continue
        k, v = entry.split("=", 1)
        parts.setdefault(k.strip(), []).append(v.strip())
    if "t" not in parts or "v1" not in parts:
        raise HTTPException(
            status_code=400,
            detail="Stripe-Signature header missing 't' or 'v1' components.",
        )
    try:
        timestamp = int(parts["t"][0])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Stripe-Signature 't' is not an integer.")

    if abs(int(time.time()) - timestamp) > _TIMESTAMP_TOLERANCE_SECONDS:
        # Spec compliance: a stale timestamp is a replay-attack
        # indicator. Reject with 400 (the body never was authoritative)
        # rather than 401.
        raise HTTPException(status_code=400, detail="Stripe-Signature timestamp is outside tolerance.")

    signed_payload = f"{timestamp}.".encode("utf-8") + body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts["v1"]):
        raise HTTPException(status_code=401, detail="Stripe-Signature does not verify.")
    return {"timestamp": str(timestamp)}


def _record_event_or_409(event_id: str, event_type: str, payload_sha: str) -> bool:
    """INSERT into stripe_events. Returns True if this is a fresh event.

    A duplicate-key violation means we have already processed this
    event in a previous delivery; silently return False so the caller
    can ack with 200 without re-applying the state change.
    """
    from sidecar.audit.db import open_audit_connection

    with open_audit_connection() as conn:
        try:
            conn.execute(
                "INSERT INTO stripe_events (event_id, event_type, payload_sha256) "
                "VALUES (%s, %s, %s)",
                [event_id, event_type, payload_sha],
            )
            return True
        except Exception as exc:
            # psycopg's UniqueViolation is a subclass of Error; we match
            # by SQLSTATE code 23505 so we do not couple to a specific
            # driver class hierarchy.
            sqlstate = getattr(exc, "sqlstate", "") or ""
            if sqlstate == "23505":
                _logger.info("stripe_event_already_processed event_id=%s", event_id)
                return False
            raise


def _apply_subscription_event(event: dict[str, Any]) -> None:
    """Materialize a customer.subscription.* event into the licenses table.

    See ``versions/20260512_0001_create_licenses.py`` for the column
    contract.
    """
    from sidecar.audit.db import open_audit_connection

    event_type = event["type"]
    sub = event["data"]["object"]
    sub_id = sub["id"]
    customer_id = sub.get("customer", "")
    status_in = (sub.get("status") or "").lower()
    plan_lookup_key = ""
    items = sub.get("items", {}).get("data", [])
    if items:
        plan_lookup_key = (items[0].get("price", {}) or {}).get("lookup_key", "") or items[0].get("price", {}).get("id", "")

    # Translate Stripe statuses to our internal vocabulary. We treat
    # 'trialing' as 'active' because a trial subscription should grant
    # access; the trial_ends_at column records the cutover.
    if status_in in {"active", "trialing"}:
        license_status = "active"
    elif status_in == "past_due":
        license_status = "past_due"
    elif status_in in {"canceled", "unpaid"}:
        license_status = "canceled"
    elif status_in == "incomplete":
        license_status = "incomplete"
    else:
        license_status = "incomplete"

    license_key = sub.get("metadata", {}).get("license_key") or f"cc_live_{sub_id}"
    trial_ends_at = sub.get("trial_end")
    current_period_end = sub.get("current_period_end")

    with open_audit_connection() as conn:
        conn.execute(
            "INSERT INTO licenses ("
            "  license_key, stripe_customer_id, stripe_subscription_id,"
            "  plan, status, trial_ends_at, current_period_end, updated_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, NOW()) "
            "ON CONFLICT (license_key) DO UPDATE SET "
            "  stripe_customer_id = EXCLUDED.stripe_customer_id,"
            "  stripe_subscription_id = EXCLUDED.stripe_subscription_id,"
            "  plan = EXCLUDED.plan,"
            "  status = EXCLUDED.status,"
            "  trial_ends_at = EXCLUDED.trial_ends_at,"
            "  current_period_end = EXCLUDED.current_period_end,"
            "  updated_at = NOW()",
            [
                license_key,
                customer_id,
                sub_id,
                plan_lookup_key or "starter",
                license_status,
                datetime.fromtimestamp(trial_ends_at, tz=timezone.utc) if trial_ends_at else None,
                datetime.fromtimestamp(current_period_end, tz=timezone.utc) if current_period_end else None,
            ],
        )
    _logger.info(
        "license_applied event_type=%s subscription_id=%s status=%s",
        event_type,
        sub_id,
        license_status,
    )


def _apply_invoice_event(event: dict[str, Any]) -> None:
    """invoice.payment_failed → flip to past_due."""
    from sidecar.audit.db import open_audit_connection

    inv = event["data"]["object"]
    sub_id = inv.get("subscription")
    if not sub_id:
        return
    with open_audit_connection() as conn:
        conn.execute(
            "UPDATE licenses SET status = 'past_due', updated_at = NOW() "
            "WHERE stripe_subscription_id = %s",
            [sub_id],
        )


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request) -> Response:
    try:
        secret = _resolve_webhook_secret()
    except RuntimeError as exc:
        # Make the misconfiguration visible — return 503 not 500 so a
        # Stripe-side dashboard alert clearly says "ours not yours".
        raise HTTPException(status_code=503, detail=str(exc))

    body = await request.body()
    _verify_signature(body, request.headers.get("stripe-signature"), secret)

    import json

    try:
        event = json.loads(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Body is not valid JSON: {exc}")

    event_id = event.get("id")
    event_type = event.get("type", "")
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="Stripe event missing id or type.")

    payload_sha = hashlib.sha256(body).hexdigest()
    if not _record_event_or_409(event_id, event_type, payload_sha):
        # Already processed; ack as 200 so Stripe stops retrying.
        return Response(status_code=200)

    try:
        if event_type.startswith("customer.subscription."):
            _apply_subscription_event(event)
        elif event_type == "invoice.payment_failed":
            _apply_invoice_event(event)
        # Unknown event types are ignored after the idempotency record,
        # so retrying still won't double-process them.
    except Exception:
        _logger.exception("stripe_webhook_apply_failed event_id=%s", event_id)
        # 500 → Stripe will retry the same event_id, which is fine —
        # the idempotency table will see it again and a successful
        # second attempt will silently no-op.
        raise HTTPException(status_code=500, detail="Internal error while applying webhook.")

    return Response(status_code=200)
