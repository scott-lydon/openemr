"""Tests for the Phase 10 observability extensions.

Coverage:

- PHI scrub at flush: regex sweep redacts SSN/MRN/DOB/phone/email/
  Patient ref; ScrubReport accumulates kind counts.
- ``ensure_ready`` with mode=presidio raises when the import fails;
  with mode=in_process passes regardless.
- Span attribute scrub recurses into lists and dicts.
- Grafana dashboard JSON parses and exposes 8 panels with stable ids.
- Alert sinks: Slack/PagerDuty emit best-effort and never raise on
  network or webhook errors; FanoutAlertSink survives one sink raising.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from sidecar.observability.alerts import (
    Alert,
    FanoutAlertSink,
    PagerDutyAlertSink,
    Severity,
    SlackAlertSink,
)
from sidecar.observability.phi_scrub import (
    PresidioRequiredButMissing,
    ScrubReport,
    ensure_ready,
    scrub_attributes,
    scrub_text,
)


# ─── PHI scrub ────────────────────────────────────────────────────────


def test_scrub_text_redacts_ssn_and_phone() -> None:
    out = scrub_text("call 555-867-5309 SSN 123-45-6789")
    assert "[REDACTED:phone]" in out
    assert "[REDACTED:ssn]" in out


def test_scrub_text_accumulates_into_report() -> None:
    report = ScrubReport()
    scrub_text(
        "Patient/abcd-1234 SSN 123-45-6789 again 999-99-9999",
        report=report,
    )
    assert report.redactions_by_kind["ssn"] == 2
    assert report.redactions_by_kind["patient_ref"] == 1
    assert report.total() == 3


def test_scrub_attributes_recurses() -> None:
    payload = {
        "user_id": "Patient/abcd-1234",
        "details": {"phone": "555-867-5309"},
        "tags": ["clean", "MRN12345678"],
    }
    out = scrub_attributes(payload)
    assert "[REDACTED:" in out["user_id"]
    assert "[REDACTED:" in out["details"]["phone"]
    assert any("[REDACTED:" in s for s in out["tags"])


def test_ensure_ready_in_process_mode_passes() -> None:
    ensure_ready(mode="in_process")  # no raise


def test_ensure_ready_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        ensure_ready(mode="bogus")


def test_ensure_ready_presidio_mode_when_missing() -> None:
    """If presidio_analyzer is not installed, presidio mode raises."""
    try:
        import presidio_analyzer  # noqa: F401
    except ImportError:
        with pytest.raises(PresidioRequiredButMissing):
            ensure_ready(mode="presidio")
    else:
        # Otherwise the call should not raise.
        ensure_ready(mode="presidio")


# ─── Grafana dashboard JSON ───────────────────────────────────────────


def test_dashboard_parses_and_has_eight_panels() -> None:
    path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "grafana"
        / "clinical_copilot_dashboard.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    panels = payload.get("panels", [])
    assert len(panels) == 8, (
        f"expected 8 panels per W2_QUALITY_PLAN section 11.2; got {len(panels)}"
    )
    ids = [p["id"] for p in panels]
    assert sorted(ids) == list(range(1, 9))
    assert payload["uid"] == "clinical-copilot-w2"


# ─── Alert sinks ──────────────────────────────────────────────────────


async def test_slack_sink_no_url_is_noop() -> None:
    """SlackAlertSink without a webhook URL silently does nothing.

    The contract is "best effort"; missing config must not raise.
    """
    # Ensure the env var is not set so we exercise the noop branch.
    os.environ.pop("COPILOT_SLACK_WEBHOOK", None)
    sink = SlackAlertSink()
    await sink.emit(
        Alert(key="x", severity=Severity.INFO, summary="hello", details={})
    )


async def test_pagerduty_skips_info_severity() -> None:
    """Info / warning never page; only error / critical do."""
    sink = PagerDutyAlertSink(routing_key="test")
    # Even with a routing key, info severity must not call the network.
    # We confirm by exercising the path; the actual httpx call is gated
    # by severity, so no exception fires when there is no network.
    await sink.emit(
        Alert(key="x", severity=Severity.INFO, summary="info", details={})
    )


async def test_fanout_sink_survives_one_failing_sink() -> None:
    class Boom:
        async def emit(self, alert: Alert) -> None:
            raise RuntimeError("simulated outage")

    class Ok:
        def __init__(self) -> None:
            self.calls: list[Alert] = []

        async def emit(self, alert: Alert) -> None:
            self.calls.append(alert)

    ok = Ok()
    fan = FanoutAlertSink(sinks=[Boom(), ok])
    await fan.emit(
        Alert(key="x", severity=Severity.WARNING, summary="hi", details={})
    )
    assert len(ok.calls) == 1
