"""create licenses table and stripe_events idempotency table

Revision ID: 20260512_0001
Revises: 20260504_0003
Create Date: 2026-05-12

Two tables, one migration, because Stripe webhook handling needs both.

``licenses``        — the row that gates /chat. One row per Stripe
                      subscription; license_key is the value the
                      clinic operator pastes into the OpenEMR module's
                      admin page.

``stripe_events``   — idempotency log. Stripe retries webhooks for ~3
                      days when our endpoint returns non-2xx; we MUST
                      treat each event_id as exactly-once so a retried
                      ``customer.subscription.created`` does not double-
                      issue a license.

Why a database table and not in-memory dedup:

- The sidecar is a stateless container. An in-memory set evaporates on
  every restart; Stripe will redeliver the same event_id and we will
  process it again. The licenses table would gain duplicate rows.
- A database table outlives every container, and is the same Postgres
  the rest of the sidecar already requires. No new dependency.

License status machine:

    active     created by customer.subscription.created
       │
       ├──► past_due       on invoice.payment_failed
       ├──► canceled       on customer.subscription.deleted
       └──► revoked        on manual admin action (no Stripe event)

The license_check() FastAPI dependency only treats ``active`` as
"good to /chat"; everything else returns HTTP 402.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260512_0001"
down_revision = "20260504_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "licenses",
        sa.Column("license_key", sa.String(length=64), primary_key=True),
        # Stripe customer + subscription identifiers. Both nullable for
        # license rows seeded by hand for internal demos.
        sa.Column("stripe_customer_id", sa.String(length=128), nullable=True, index=True),
        sa.Column(
            "stripe_subscription_id",
            sa.String(length=128),
            nullable=True,
            unique=True,
        ),
        # Plan identifier — e.g. "starter", "pro", "enterprise". The
        # license_check() dependency does not branch on this today,
        # but the audit log records it for billing reconciliation.
        sa.Column("plan", sa.String(length=32), nullable=False, server_default=sa.text("'starter'")),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
            comment="One of: active, past_due, canceled, revoked, incomplete.",
        ),
        # Seat counts are advisory; the gate is binary (paid or not).
        # The Pro tier promises N seats per organisation but enforcement
        # is downstream in OpenEMR's own user-management.
        sa.Column("seats_purchased", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('active','past_due','canceled','revoked','incomplete')",
            name="ck_licenses_status",
        ),
    )

    op.create_table(
        "stripe_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False, comment="Hex SHA-256 of the raw body so a replay with a tampered body is detectable."),
    )

    # An index on stripe_events.processed_at for "events older than N
    # days can be vacuumed" pruning runs.
    op.create_index(
        "ix_stripe_events_processed_at",
        "stripe_events",
        ["processed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_stripe_events_processed_at", table_name="stripe_events")
    op.drop_table("stripe_events")
    op.drop_table("licenses")
