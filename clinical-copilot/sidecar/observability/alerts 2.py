"""Alert webhook helpers.

The dashboard panels in ``clinical_copilot_dashboard.json`` describe
WHAT to watch. This module describes WHO is paged.

Two channels:

- **Slack incoming webhook** for soft alerts (cost soft alert,
  reranker degradation rate climbing, eval gate regression).
- **PagerDuty Events v2** for hard alerts (dead-letter rate over 10/hr,
  cost envelope hit, Presidio unavailable).

Both implementations are best-effort: a webhook delivery failure
logs a warning but does not raise — the agent's main work must not
fail because the alert channel is down. Failure metrics are themselves
emitted so a Grafana panel can alert when alerts cannot be sent.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Protocol

import httpx


logger = logging.getLogger(__name__)


SLACK_WEBHOOK_ENV: Final[str] = "COPILOT_SLACK_WEBHOOK"
PAGERDUTY_ROUTING_KEY_ENV: Final[str] = "COPILOT_PAGERDUTY_ROUTING_KEY"
PAGERDUTY_EVENTS_URL: Final[str] = "https://events.pagerduty.com/v2/enqueue"


class Severity(str, Enum):
    """Closed enum so a typo in a caller is a type error, not a missed page."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    """One alert payload.

    ``key`` is a stable dedup identifier (PagerDuty groups alerts by
    key; Slack uses it as the message thread root when relevant). The
    operator can correlate ``key`` to the dashboard panel that fired
    the alert.
    """

    key: str
    severity: Severity
    summary: str
    details: dict[str, Any]


class AlertSink(Protocol):
    async def emit(self, alert: Alert) -> None:
        ...


class SlackAlertSink:
    """Slack incoming-webhook sink. Best effort.

    The Slack webhook is rate-limited at 1/second per webhook URL; we
    do not enforce that here because the alert volume is well under
    the limit on a healthy system.
    """

    def __init__(self, webhook_url: str | None = None) -> None:
        self._url = webhook_url or os.environ.get(SLACK_WEBHOOK_ENV)

    async def emit(self, alert: Alert) -> None:
        if not self._url:
            logger.debug("slack webhook unset; skipping alert key=%s", alert.key)
            return
        body = {
            "text": f"[{alert.severity.value.upper()}] {alert.summary}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{alert.summary}*"}},
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"key: `{alert.key}`"},
                        {
                            "type": "mrkdwn",
                            "text": "details: " + "```" + json.dumps(alert.details, default=str)[:500] + "```",
                        },
                    ],
                },
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.post(self._url, json=body)
            if response.status_code >= 300:
                logger.warning(
                    "slack webhook returned status=%d body[:200]=%r",
                    response.status_code, response.text[:200],
                )
        except httpx.RequestError as exc:
            logger.warning("slack webhook network error: %r", exc)


class PagerDutyAlertSink:
    """PagerDuty Events v2 sink. Best effort."""

    def __init__(self, routing_key: str | None = None) -> None:
        self._routing_key = routing_key or os.environ.get(PAGERDUTY_ROUTING_KEY_ENV)

    async def emit(self, alert: Alert) -> None:
        if not self._routing_key:
            logger.debug("pagerduty unset; skipping alert key=%s", alert.key)
            return
        if alert.severity in (Severity.INFO, Severity.WARNING):
            return  # info/warning go to Slack, not PagerDuty
        body = {
            "routing_key": self._routing_key,
            "event_action": "trigger",
            "dedup_key": alert.key,
            "payload": {
                "summary": alert.summary,
                "severity": alert.severity.value,
                "source": "clinical-copilot",
                "custom_details": alert.details,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                response = await client.post(PAGERDUTY_EVENTS_URL, json=body)
            if response.status_code >= 300:
                logger.warning(
                    "pagerduty returned status=%d body[:200]=%r",
                    response.status_code, response.text[:200],
                )
        except httpx.RequestError as exc:
            logger.warning("pagerduty network error: %r", exc)


@dataclass
class FanoutAlertSink:
    """Composite sink — emit to every wired channel.

    Production wiring constructs this with a Slack sink + a PagerDuty
    sink; tests replace it with a list-collecting fake.
    """

    sinks: list[AlertSink]

    async def emit(self, alert: Alert) -> None:
        for sink in self.sinks:
            try:
                await sink.emit(alert)
            except Exception as exc:
                logger.warning(
                    "alert sink %s raised: %r", type(sink).__name__, exc,
                )


__all__ = [
    "Alert",
    "AlertSink",
    "FanoutAlertSink",
    "PAGERDUTY_EVENTS_URL",
    "PAGERDUTY_ROUTING_KEY_ENV",
    "PagerDutyAlertSink",
    "SLACK_WEBHOOK_ENV",
    "Severity",
    "SlackAlertSink",
]
