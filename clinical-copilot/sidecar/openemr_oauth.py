"""OpenEMR OAuth2 client_credentials helper for the sidecar.

The sidecar speaks to OpenEMR's FHIR R4 surface as a system-scoped backend
service (SMART-on-FHIR Backend Services). Each call needs a short-lived
access token; this module mints them with the ``client_credentials`` grant
and caches the result until ~30 seconds before expiry to avoid a token
fetch per FHIR request.

Configuration:

- ``COPILOT_OPENEMR_OAUTH_BASE`` — e.g. ``http://5.161.253.237/oauth2/default``
- ``COPILOT_OPENEMR_CLIENT_ID``  — registered in OpenEMR Admin → API Clients
- ``COPILOT_OPENEMR_CLIENT_SECRET`` — same registration; required

Failures throw a single :class:`OpenEMRTokenError` with a precise message
naming the OAuth endpoint, status code, and the OpenEMR error body so a
broken client registration is diagnosable from the log line alone.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Final

import httpx

from sidecar.config import Settings


_REFRESH_LEEWAY_SECONDS: Final[int] = 30
_REQUEST_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=3.0, read=10.0, write=10.0, pool=5.0
)


# System-scoped FHIR scopes the sidecar requests. Trimmed to exactly what
# DEFAULT_RESOURCE_QUERIES in fhir_client.py reads — least-privilege.
SYSTEM_FHIR_SCOPES: Final[tuple[str, ...]] = (
    "system/Patient.read",
    "system/Condition.read",
    "system/MedicationRequest.read",
    "system/AllergyIntolerance.read",
    "system/Observation.read",
    "system/Encounter.read",
    "system/Procedure.read",
    "system/DocumentReference.read",
)


class OpenEMRTokenError(RuntimeError):
    """OAuth2 client_credentials grant failed.

    The message includes the endpoint URL, HTTP status, and the first 500
    chars of the response body so a misconfigured client (wrong secret,
    missing scope grant, OpenEMR side error) is diagnosable from one log
    line. Never include the client secret itself in the message.
    """

    def __init__(self, *, endpoint: str, status: int, body: str) -> None:
        super().__init__(
            f"OpenEMR token endpoint {endpoint} returned HTTP {status}: {body[:500]}"
        )
        self.endpoint = endpoint
        self.status = status
        self.body = body


class OpenEMRConfigurationError(RuntimeError):
    """Client_credentials cannot be attempted because config is missing.

    Raised before any HTTP call so the failure mode is unambiguous.
    """


@dataclass(frozen=True)
class _CachedToken:
    """Internal cache entry for a system access token."""

    access_token: str
    expires_at: int  # unix seconds


class OpenEMRTokenCache:
    """Process-wide cache for OpenEMR client_credentials tokens.

    A single cache entry is shared across all FHIR fan-outs in this
    process. Refreshes are serialised with an asyncio.Lock so a burst of
    concurrent requests doesn't trigger N parallel token fetches.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.openemr_client_id:
            raise OpenEMRConfigurationError(
                "COPILOT_OPENEMR_CLIENT_ID is empty; register a client at "
                "OpenEMR Admin → System → API Clients and set the env var"
            )
        if not settings.openemr_client_secret:
            raise OpenEMRConfigurationError(
                "COPILOT_OPENEMR_CLIENT_SECRET is empty; copy the secret "
                "shown after API client registration into the sidecar .env"
            )
        self._settings = settings
        self._cached: _CachedToken | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        """Return a valid access token, refreshing if needed."""
        now = int(time.time())
        cached = self._cached
        if cached is not None and cached.expires_at - _REFRESH_LEEWAY_SECONDS > now:
            return cached.access_token
        async with self._lock:
            # Re-check inside the lock — another coroutine may have refreshed.
            cached = self._cached
            if cached is not None and cached.expires_at - _REFRESH_LEEWAY_SECONDS > now:
                return cached.access_token
            self._cached = await self._fetch_new()
            return self._cached.access_token

    async def _fetch_new(self) -> _CachedToken:
        endpoint = f"{self._settings.openemr_oauth_base.rstrip('/')}/token"
        scope = " ".join(SYSTEM_FHIR_SCOPES)
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT, verify=self._settings.fhir_verify_ssl
        ) as client:
            response = await client.post(
                endpoint,
                data={
                    "grant_type": "client_credentials",
                    "scope": scope,
                },
                auth=(
                    self._settings.openemr_client_id,
                    self._settings.openemr_client_secret or "",
                ),
                headers={"Accept": "application/json"},
            )
        if response.status_code >= 400:
            raise OpenEMRTokenError(
                endpoint=endpoint,
                status=response.status_code,
                body=response.text,
            )
        body = response.json()
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OpenEMRTokenError(
                endpoint=endpoint,
                status=response.status_code,
                body=f"missing access_token in response: {body!r}",
            )
        expires_in = int(body.get("expires_in", 3600))
        return _CachedToken(
            access_token=access_token,
            expires_at=int(time.time()) + expires_in,
        )


__all__ = [
    "OpenEMRConfigurationError",
    "OpenEMRTokenCache",
    "OpenEMRTokenError",
    "SYSTEM_FHIR_SCOPES",
]
