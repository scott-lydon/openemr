"""Tests for the Phase 12 deployment-hardening primitives.

Coverage:

- ShutdownCoordinator: request_shutdown sets the event idempotently;
  is_shutting_down reflects state; await_drain returns when tasks
  finish; await_drain times out cleanly when a task hangs.
- TokenBucketRateLimiter: capacity is the burst limit; refill follows
  the configured per-minute rate; refill is monotonic; empty key
  rejected; per-key buckets are independent.
- k6 scenario files exist and parse as JS (smoke check that the file
  is syntactically present, not a deep parse).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from sidecar.observability.rate_limiter import (
    DEFAULT_PER_TOKEN_BURST,
    TokenBucketRateLimiter,
)
from sidecar.observability.shutdown import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    ShutdownCoordinator,
)


# ─── Shutdown coordinator ────────────────────────────────────────────


async def test_shutdown_event_starts_unset() -> None:
    coord = ShutdownCoordinator()
    assert not coord.is_shutting_down()


async def test_request_shutdown_is_idempotent() -> None:
    coord = ShutdownCoordinator()
    coord.request_shutdown()
    coord.request_shutdown()  # second call must not raise
    assert coord.is_shutting_down()


async def test_await_drain_returns_when_tasks_finish() -> None:
    coord = ShutdownCoordinator()

    async def quick():
        await asyncio.sleep(0.01)

    coord.track(asyncio.create_task(quick()))
    await coord.await_drain(timeout_seconds=1.0)


async def test_await_drain_times_out_cleanly() -> None:
    coord = ShutdownCoordinator()

    async def hang():
        await asyncio.sleep(60)

    task = asyncio.create_task(hang())
    coord.track(task)
    t0 = time.monotonic()
    await coord.await_drain(timeout_seconds=0.05)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"drain blocked too long: {elapsed:.2f}s"
    task.cancel()


# ─── Rate limiter ────────────────────────────────────────────────────


def test_rate_limiter_allows_burst_up_to_capacity() -> None:
    limiter = TokenBucketRateLimiter(capacity=5, refill_per_minute=60)
    for _ in range(5):
        assert limiter.allow("k") is True
    assert limiter.allow("k") is False


def test_rate_limiter_refills_over_time() -> None:
    limiter = TokenBucketRateLimiter(capacity=2, refill_per_minute=600)
    # Drain.
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    # Refill rate is 10/sec; sleep 0.3s → ~3 tokens, capped at 2.
    time.sleep(0.3)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is True


def test_rate_limiter_per_key_buckets_are_independent() -> None:
    limiter = TokenBucketRateLimiter(capacity=1, refill_per_minute=60)
    assert limiter.allow("a") is True
    # bucket "a" exhausted; bucket "b" still full.
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True


def test_rate_limiter_rejects_empty_key() -> None:
    limiter = TokenBucketRateLimiter()
    with pytest.raises(ValueError):
        limiter.allow("")


# ─── k6 scenario files ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "scenario", ["baseline.js", "peak.js", "surge.js"],
)
def test_k6_scenario_file_exists_and_imports_k6_http(scenario: str) -> None:
    path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "tests" / "load" / scenario
    )
    text = path.read_text(encoding="utf-8")
    assert "import http from 'k6/http'" in text
    assert "export default function" in text
    assert "thresholds" in text


def test_default_drain_timeout_is_thirty_seconds() -> None:
    """Documented value; tests this rather than memorizing it."""
    assert DEFAULT_DRAIN_TIMEOUT_SECONDS == 30.0


def test_default_burst_matches_documented_value() -> None:
    assert DEFAULT_PER_TOKEN_BURST == 30
