"""License key generator for Clinical Co-Pilot.

The user-facing entry points are:

1. **Library** — ``generate_license_key(GenerateLicenseInput(...))`` returns
   a ``GenerateLicenseOutput`` after inserting a row in the ``licenses``
   table. Use this from any internal admin script.

2. **CLI** — ``python -m sidecar.licensing.generate --plan pro ...``
   prints the new key, status, and trial-end timestamp to stdout. Use
   this to issue trial keys before Stripe is wired up (Section 1.2 of
   ``USER_ACTION_HANDOFF.md``), and to mint keys for customers who pay
   out-of-band (wire transfer, invoice, partner deal).

3. **HTTP** — ``POST /admin/licenses`` in ``sidecar.api.admin_licenses``
   wraps this same function. The HTTP path is only enabled when
   ``COPILOT_ADMIN_TOKEN`` is set on the sidecar; otherwise it refuses
   every request so a forgotten config does not silently expose key
   issuance to the world.

License key format
==================

``lic_<32 lowercase hex chars>`` — 128 bits of entropy plus a static
prefix so the key is recognisable in operator paste-buffers and log
greps. Example: ``lic_a1b2c3d4e5f6071829354c5a7b8d9e0f``.

The ``lic_`` prefix is purely cosmetic. The 128 random bits are what
make the key unguessable.

Failure modes
=============

Every failure raises ``GenerateLicenseError`` with a message that tells
the operator exactly what to fix. We intentionally do not wrap or
swallow these; both the CLI and the HTTP layer print/return the message
verbatim because the diagnostic value of the original is higher than
any post-hoc rewrap.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

Plan = Literal["starter", "pro", "enterprise"]
Status = Literal["active", "past_due", "canceled", "revoked", "incomplete"]

_VALID_PLANS: tuple[Plan, ...] = ("starter", "pro", "enterprise")
_VALID_STATUSES: tuple[Status, ...] = (
    "active",
    "past_due",
    "canceled",
    "revoked",
    "incomplete",
)

_logger = logging.getLogger(__name__)


class GenerateLicenseError(RuntimeError):
    """Raised when license generation fails for any reason.

    The message must be self-contained — it is surfaced verbatim in
    CLI stderr and in HTTP error bodies. Include the cause AND a
    remediation hint.
    """


@dataclasses.dataclass(frozen=True)
class GenerateLicenseInput:
    """Input to ``generate_license_key``. All fields validated up front."""

    plan: Plan
    customer_email: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    seats: int = 1
    trial_days: Optional[int] = None
    status: Status = "active"


@dataclasses.dataclass(frozen=True)
class GenerateLicenseOutput:
    """The freshly-minted row, returned so the caller can echo it."""

    license_key: str
    plan: Plan
    status: Status
    seats: int
    trial_ends_at: Optional[datetime]
    customer_email: Optional[str]


def _mint_license_key() -> str:
    """Mint a fresh key.

    ``secrets.token_hex(16)`` yields 128 bits of entropy from the
    system CSPRNG. Combined with the ``lic_`` prefix the key is 36
    chars total, comfortably under the 64-char limit on the
    ``licenses.license_key`` column.
    """
    return f"lic_{secrets.token_hex(16)}"


def _validate(inp: GenerateLicenseInput) -> None:
    """Reject malformed input with a message that names the fix.

    Every check identifies the offending field, the rule it violated,
    and the exact CLI flag the operator should change.
    """
    if inp.plan not in _VALID_PLANS:
        raise GenerateLicenseError(
            f"plan must be one of {list(_VALID_PLANS)!r}, got {inp.plan!r}. "
            "Pass --plan starter, --plan pro, or --plan enterprise."
        )
    if inp.status not in _VALID_STATUSES:
        raise GenerateLicenseError(
            f"status must be one of {list(_VALID_STATUSES)!r}, got "
            f"{inp.status!r}. Pass --status active for a new key."
        )
    if inp.seats < 1:
        raise GenerateLicenseError(
            f"seats must be >= 1, got {inp.seats}. Pass --seats N with N >= 1."
        )
    if inp.trial_days is not None and inp.trial_days < 0:
        raise GenerateLicenseError(
            f"trial-days must be >= 0, got {inp.trial_days}. "
            "Omit --trial-days for a non-trial license, or pass a "
            "non-negative integer."
        )


def generate_license_key(inp: GenerateLicenseInput) -> GenerateLicenseOutput:
    """Generate a license key and insert it into the licenses table.

    Returns the new ``GenerateLicenseOutput`` so the caller can echo,
    email, or otherwise hand the key back to the customer.

    Side effects:
        - One INSERT into ``licenses``.
        - One INFO log line with the generated key truncated for safety.

    Raises:
        GenerateLicenseError: any failure. The message is operator-ready.
    """
    _validate(inp)

    license_key = _mint_license_key()
    trial_ends_at: Optional[datetime] = None
    if inp.trial_days is not None and inp.trial_days > 0:
        trial_ends_at = datetime.now(timezone.utc) + timedelta(days=inp.trial_days)

    # Import lazily so importing this module from a tooling context does
    # not eagerly require the audit DB driver. The error message names
    # the most common cause when the import fails.
    try:
        from sidecar.audit.db import open_audit_connection
    except ImportError as exc:
        raise GenerateLicenseError(
            f"Cannot import sidecar.audit.db: {exc}. The license generator "
            "needs the sidecar package on the PYTHONPATH. Run from the "
            "sidecar root directory (so 'sidecar' is importable), or pip "
            "install -e the sidecar package."
        ) from exc

    try:
        with open_audit_connection() as conn:
            conn.execute(
                """
                INSERT INTO licenses (
                    license_key,
                    stripe_customer_id,
                    plan,
                    status,
                    seats_purchased,
                    trial_ends_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    license_key,
                    inp.stripe_customer_id,
                    inp.plan,
                    inp.status,
                    inp.seats,
                    trial_ends_at,
                ],
            )
    except Exception as exc:
        raise GenerateLicenseError(
            f"INSERT into licenses failed: {exc}. Common causes: the "
            "licenses table does not exist (run 'alembic upgrade head' "
            "from clinical-copilot/sidecar/); COPILOT_DATABASE_URL is "
            "not set or points at the wrong database; the database is "
            "unreachable; or stripe_customer_id collides with an "
            "existing unique row."
        ) from exc

    _logger.info(
        "license_key_generated",
        extra={
            # Log a truncated form of the key so support can match a
            # customer's key to a log line without putting the full
            # secret in the log. The last 4 chars are enough to
            # disambiguate while remaining safe to keep.
            "license_key_suffix": license_key[-6:],
            "plan": inp.plan,
            "status": inp.status,
            "seats": inp.seats,
            "trial_days": inp.trial_days,
            "stripe_customer_id": inp.stripe_customer_id,
        },
    )

    return GenerateLicenseOutput(
        license_key=license_key,
        plan=inp.plan,
        status=inp.status,
        seats=inp.seats,
        trial_ends_at=trial_ends_at,
        customer_email=inp.customer_email,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point.

    Usage::

        python -m sidecar.licensing.generate \\
            --plan pro \\
            --customer-email user@example.com \\
            --trial-days 14
    """
    parser = argparse.ArgumentParser(
        prog="python -m sidecar.licensing.generate",
        description=(
            "Generate a Clinical Co-Pilot license key, insert it into "
            "the licenses table, and print it. Use to issue trial keys "
            "before Stripe is wired up, or to mint keys for out-of-band "
            "deals."
        ),
    )
    parser.add_argument(
        "--plan",
        required=True,
        choices=list(_VALID_PLANS),
        help="Tier the key entitles.",
    )
    parser.add_argument(
        "--customer-email",
        help=(
            "Customer email for your records. NOT stored in the "
            "licenses row (the row stores stripe_customer_id only); "
            "this is just echoed back so you can paste it into the "
            "welcome email."
        ),
    )
    parser.add_argument(
        "--stripe-customer-id",
        help="Stripe customer ID if known. Optional for non-Stripe trials.",
    )
    parser.add_argument(
        "--seats",
        type=int,
        default=1,
        help="Seats purchased. Default 1.",
    )
    parser.add_argument(
        "--trial-days",
        type=int,
        default=None,
        help="If set, key is a trial that expires after N days.",
    )
    parser.add_argument(
        "--status",
        default="active",
        choices=list(_VALID_STATUSES),
        help="Initial row status. Default 'active'.",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    inp = GenerateLicenseInput(
        plan=args.plan,
        customer_email=args.customer_email,
        stripe_customer_id=args.stripe_customer_id,
        seats=args.seats,
        trial_days=args.trial_days,
        status=args.status,
    )
    try:
        out = generate_license_key(inp)
    except GenerateLicenseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print()
    print("=" * 64)
    print("License key generated successfully")
    print("=" * 64)
    print(f"  license_key:    {out.license_key}")
    print(f"  plan:           {out.plan}")
    print(f"  status:         {out.status}")
    print(f"  seats:          {out.seats}")
    if out.customer_email:
        print(f"  customer email: {out.customer_email}  (echoed; not stored)")
    if out.trial_ends_at:
        print(f"  trial_ends_at:  {out.trial_ends_at.isoformat()}")
    print()
    print("Paste the license_key into the customer's OpenEMR module Globals:")
    print("  Administration > Globals > Clinical Co-Pilot > License key")
    print("OR set COPILOT_LICENSE_KEY in the sidecar's .env if self-hosting.")
    print("=" * 64)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
