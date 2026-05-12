"""Token bucket rate limiter.

Per-token + per-Internet Protocol (IP) rate limiting for the agent
gateway. The token bucket model:

- Each bucket has a capacity (max burst) and a refill rate (steady
  state requests per second).
- An incoming request consumes one token. If no tokens, the request is
  rejected with HTTP 429.
- Buckets refill continuously: ``refill_rate * elapsed_seconds``
  tokens added per call.

Why token bucket rather than fixed-window:

- Token bucket is the standard for API rate limiting because it
  allows controlled burst (a clinician opening a chart with 5 attached
  documents in quick succession should not hit the limit) while
  enforcing steady-state throughput.

In-memory implementation only: production multi-process deployments
should swap in a Redis-backed bucket. The protocol seam is here for
that.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Final, Protocol


DEFAULT_PER_TOKEN_PER_MINUTE: Final[int] = 120
DEFAULT_PER_TOKEN_BURST: Final[int] = 30


class RateLimiter(Protocol):
    """Protocol every rate-limiter implementation honors."""

    def allow(self, key: str) -> bool:
        ...


@dataclass
class _Bucket:
    capacity: float
    refill_per_second: float
    tokens: float
    last_refill_unix: float


@dataclass
class TokenBucketRateLimiter:
    """Process-local token-bucket implementation.

    ``capacity`` is the burst size; ``refill_per_minute`` is the steady
    rate. Buckets are kept in a dict keyed on ``key`` (e.g. the task
    token's ``jti`` claim or the request's source IP).
    """

    capacity: float = DEFAULT_PER_TOKEN_BURST
    refill_per_minute: float = DEFAULT_PER_TOKEN_PER_MINUTE
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        if not key:
            raise ValueError(
                "RateLimiter.allow received an empty key; the caller must "
                "produce a stable identifier (token jti, source IP, etc.)"
            )

        refill_per_second = self.refill_per_minute / 60.0
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(
                    capacity=self.capacity,
                    refill_per_second=refill_per_second,
                    tokens=self.capacity,
                    last_refill_unix=now,
                )
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket.last_refill_unix
                if elapsed > 0:
                    bucket.tokens = min(
                        bucket.capacity,
                        bucket.tokens + elapsed * bucket.refill_per_second,
                    )
                    bucket.last_refill_unix = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False


__all__ = [
    "DEFAULT_PER_TOKEN_BURST",
    "DEFAULT_PER_TOKEN_PER_MINUTE",
    "RateLimiter",
    "TokenBucketRateLimiter",
]
