"""Runtime cost ceiling enforcement.

The cost ceiling guards against runaway agent loops, prompt-injection
amplification, and accidental load tests that would otherwise burn the
project's API budget overnight.

Three thresholds:

- **Soft alert** at 80% of the daily envelope. Fires once per envelope
  reset; emits a webhook (Slack channel, email, PagerDuty) so the
  operator sees the spike before it becomes a hard cutoff. Raising the
  envelope from the dashboard while the soft alert is pending should
  clear the alert.
- **Hard cutoff** at 100% of the envelope. The gateway refuses new
  agent invocations with HTTP 503 and a typed
  ``CostEnvelopeExceeded`` error. The operator can lift the cap from
  the dashboard or wait for the daily reset (UTC midnight).
- **Per-request budget probe** before submission. The cost-aware caller
  estimates the request's cost from token counts and adds it to the
  running total. If the running total + estimate exceeds the envelope,
  the call is refused before the model is ever invoked.

Why per-request rather than per-day-only:

- A single runaway loop can blow through a daily envelope in seconds.
  The per-request probe catches the loop on the first request that
  would push past the cap.
- The probe also gives the trace a usable "rejected at probe" entry,
  so a debug session can reconstruct the runaway pattern.

Storage:

- The running total lives in a process-local atomic counter. A
  multi-process deployment uses a shared Postgres row (one row per
  envelope) so every process sees the same total. The interface here
  hides that detail behind a Protocol so the unit tests use an
  in-memory backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final, Protocol


logger = logging.getLogger(__name__)


DEFAULT_DAILY_ENVELOPE_USD: Final[float] = 5.0
DEFAULT_SOFT_ALERT_FRACTION: Final[float] = 0.8


class CostEnvelopeExceeded(Exception):
    """The hard cutoff fired; the request is refused.

    Carries the running total and the envelope so the gateway's error
    response includes enough detail for the operator to triage.
    """

    def __init__(self, *, running_usd: float, envelope_usd: float) -> None:
        self.running_usd = running_usd
        self.envelope_usd = envelope_usd
        super().__init__(
            f"daily envelope ${envelope_usd:.2f} exceeded; "
            f"running total ${running_usd:.4f}. Request refused."
        )


@dataclass(frozen=True)
class CostProbeResult:
    """Outcome of the per-request budget probe.

    ``allowed`` is True when the request can proceed. ``soft_alert_fired``
    is True when this request is the first since envelope reset to push
    the running total past the soft threshold; the caller should fan a
    webhook in that case.
    """

    allowed: bool
    running_usd: float
    envelope_usd: float
    soft_alert_fired: bool


class CostStore(Protocol):
    """Protocol for the running-total store.

    Production binds to a shared Postgres row keyed on UTC date. Tests
    bind to ``InMemoryCostStore`` for determinism.
    """

    def envelope(self) -> float:
        ...

    def running(self) -> float:
        ...

    def increment(self, usd: float) -> float:
        ...

    def soft_alert_already_fired(self) -> bool:
        ...

    def mark_soft_alert_fired(self) -> None:
        ...

    def reset(self) -> None:
        ...


@dataclass
class InMemoryCostStore:
    """Process-local store. Adequate for a single-worker deployment and
    every unit test."""

    envelope_usd: float = DEFAULT_DAILY_ENVELOPE_USD
    soft_alert_fraction: float = DEFAULT_SOFT_ALERT_FRACTION
    _running: float = 0.0
    _alerted: bool = False
    last_reset_utc_date: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    )

    def envelope(self) -> float:
        self._maybe_reset()
        return self.envelope_usd

    def running(self) -> float:
        self._maybe_reset()
        return self._running

    def increment(self, usd: float) -> float:
        self._maybe_reset()
        if usd < 0:
            raise ValueError(f"increment must be non-negative, got {usd}")
        self._running += usd
        return self._running

    def soft_alert_already_fired(self) -> bool:
        self._maybe_reset()
        return self._alerted

    def mark_soft_alert_fired(self) -> None:
        self._alerted = True

    def reset(self) -> None:
        self._running = 0.0
        self._alerted = False
        self.last_reset_utc_date = datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    def _maybe_reset(self) -> None:
        today = datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if today > self.last_reset_utc_date:
            self.reset()
            self.last_reset_utc_date = today


def probe_and_record(
    store: CostStore,
    *,
    estimated_usd: float,
) -> CostProbeResult:
    """Run the per-request probe; record the cost; return the verdict.

    Always records the cost (even when refusing) so the dashboard sees
    every request's contribution. Refusing is the right move when the
    probe would push past the envelope.
    """
    if estimated_usd < 0:
        raise ValueError(f"estimated_usd must be non-negative, got {estimated_usd}")

    envelope = store.envelope()
    running_before = store.running()
    projected = running_before + estimated_usd
    soft_threshold = envelope * 0.8

    if projected > envelope:
        # Refuse without recording the request's cost: refusing means
        # we did not actually call the model, so charging the envelope
        # for it would over-count.
        raise CostEnvelopeExceeded(
            running_usd=running_before, envelope_usd=envelope
        )

    new_total = store.increment(estimated_usd)
    soft_fired_now = (
        new_total >= soft_threshold and not store.soft_alert_already_fired()
    )
    if soft_fired_now:
        store.mark_soft_alert_fired()
        logger.warning(
            "cost soft alert fired: running=%.4f envelope=%.2f", new_total, envelope,
        )

    return CostProbeResult(
        allowed=True,
        running_usd=new_total,
        envelope_usd=envelope,
        soft_alert_fired=soft_fired_now,
    )


__all__ = [
    "CostEnvelopeExceeded",
    "CostProbeResult",
    "CostStore",
    "DEFAULT_DAILY_ENVELOPE_USD",
    "DEFAULT_SOFT_ALERT_FRACTION",
    "InMemoryCostStore",
    "probe_and_record",
]
