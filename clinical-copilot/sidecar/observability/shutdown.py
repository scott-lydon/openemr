"""Graceful shutdown coordination.

The sidecar runs a long-lived queue worker plus a FastAPI HTTP
server. Both must drain on SIGTERM:

- The worker finishes the current job, commits, exits the loop.
- The HTTP server stops accepting new requests, waits for in-flight
  requests to finish, then closes.

Coordination is one ``asyncio.Event`` (``stop_event``); SIGTERM sets
it, every long-lived task waits on it, and ``await_drain`` polls until
all tasks complete or a timeout fires.

Why a separate module rather than inline ``signal.signal`` calls:

- Tests need to drive the same lifecycle deterministically without
  actually sending signals to the process. The module exposes
  ``request_shutdown`` so a test can trigger the same code path that
  SIGTERM does.
- ``asyncio`` signal-handling differs slightly between Linux, macOS,
  and Windows. One module owns the dance.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Final


logger = logging.getLogger(__name__)


# Drain timeout. After this many seconds the process exits even if
# tasks have not finished. Long enough for a queue job mid-flight to
# complete; short enough that a stuck task does not block a deploy.
DEFAULT_DRAIN_TIMEOUT_SECONDS: Final[float] = 30.0


class ShutdownCoordinator:
    """Hold the global stop event and coordinate drain.

    Construction is cheap; install the signal handlers explicitly via
    ``install_signal_handlers`` once at startup, after the event loop
    is running.
    """

    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task[object]] = []

    @property
    def stop_event(self) -> asyncio.Event:
        return self._stop_event

    def is_shutting_down(self) -> bool:
        return self._stop_event.is_set()

    def request_shutdown(self) -> None:
        """Trigger drain. Idempotent."""
        if not self._stop_event.is_set():
            logger.info("shutdown requested; draining tasks")
            self._stop_event.set()

    def track(self, task: asyncio.Task[object]) -> None:
        """Register a task to wait on during drain."""
        self._tasks.append(task)

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach SIGTERM and SIGINT handlers.

        Call once at startup. Wrapped in try/except because signal
        handler attachment is platform-specific (Windows + add-only
        loops) and we never want the handler-install to crash boot.
        """
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except (NotImplementedError, RuntimeError) as exc:
                logger.warning(
                    "could not install handler for %s: %s; the process "
                    "will not drain on that signal.",
                    sig.name, exc,
                )

    async def await_drain(
        self, *, timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS
    ) -> None:
        """Wait for tracked tasks to finish, up to ``timeout_seconds``.

        After the timeout, return with a warning. The caller (typically
        ``main``) should then exit the process.
        """
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "drain timed out after %.1fs; %d task(s) did not finish",
                timeout_seconds,
                sum(1 for t in self._tasks if not t.done()),
            )


__all__ = ["DEFAULT_DRAIN_TIMEOUT_SECONDS", "ShutdownCoordinator"]
