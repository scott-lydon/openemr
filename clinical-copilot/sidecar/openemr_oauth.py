"""OpenEMR SMART Backend Services token helper for the sidecar.

The sidecar speaks to OpenEMR's FHIR R4 surface as a system-scoped
backend service per the SMART App Launch v2 Backend Services profile
(https://hl7.org/fhir/smart-app-launch/backend-services.html). Each
call needs a short-lived access token; this module mints them with the
``client_credentials`` grant + ``jwt-bearer`` client assertion (RFC 7523)
and caches the result until ~30 seconds before expiry to avoid a token
fetch per FHIR request.

Why JWT-bearer rather than HTTP Basic with a client_secret:
OpenEMR's ``CustomClientCredentialsGrant`` only accepts the JWT-bearer
flow. A request with HTTP Basic auth gets back HTTP 400 with
``"assertion type is not supported"``. The sidecar therefore signs a
short-lived assertion JWT with its private RSA key and the OpenEMR
side verifies it against the public JWKS registered on the
``oauth_clients`` row.

Configuration:

- ``COPILOT_OPENEMR_OAUTH_BASE`` — e.g. ``http://localhost:8300/oauth2/default``
- ``COPILOT_OPENEMR_CLIENT_ID``  — written by ``setup-openemr-client.sh``
                                   into ``.env`` after provisioning
- ``COPILOT_OPENEMR_PRIVATE_KEY_PATH`` — filesystem path to the RSA
                                   private key PEM file. Generated and
                                   pointed at by the setup script.

Failures throw a single :class:`OpenEMRTokenError` with a precise message
naming the OAuth endpoint, status code, and the OpenEMR error body so a
broken client registration is diagnosable from the log line alone.
The private key bytes are NEVER included in any error message.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
import jwt

from sidecar.config import Settings


_REFRESH_LEEWAY_SECONDS: Final[int] = 30
_REQUEST_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=3.0, read=10.0, write=10.0, pool=5.0
)

# RFC 7523 §2.2 — the assertion lifetime should be short. SMART
# Backend Services recommends ≤ 5 minutes; we use 4 to leave headroom
# for clock skew between the sidecar and the OpenEMR host.
_ASSERTION_LIFETIME_SECONDS: Final[int] = 240

# RFC 7523 §2.2 — the assertion type identifier for jwt-bearer.
_JWT_BEARER_ASSERTION_TYPE: Final[str] = (
    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
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
    # write scope is needed for the chat upload's "drop a PDF, see it
    # in the patient profile" demo path. Without it the FHIR DocumentReference
    # POST gets HTTP 404 / 'Route not found' even though the
    # CapabilityStatement advertises 'create' — OpenEMR registers the
    # POST handler conditionally on the scope grant.
    "system/DocumentReference.write",
)


class OpenEMRTokenError(RuntimeError):
    """OAuth2 client_credentials grant failed.

    The message includes the endpoint URL, HTTP status, and the first 500
    chars of the response body so a misconfigured client (wrong JWKS,
    expired key, missing scope grant, OpenEMR side error) is diagnosable
    from one log line. NEVER includes the private key in the message.
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
    Each subclass-style message names exactly one missing thing so the
    operator's next step is unambiguous (run setup-openemr-client.sh,
    fix a permission, etc.).
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

    The private RSA key is read once at construction and held in
    memory; the file is never re-read unless the process restarts. This
    avoids a per-request stat() call and means key rotation requires a
    sidecar restart (the same restart that re-reads .env).
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.openemr_client_id:
            raise OpenEMRConfigurationError(
                "COPILOT_OPENEMR_CLIENT_ID is empty; run "
                "clinical-copilot/scripts/setup-openemr-client.sh to "
                "provision a Backend Services client and populate .env"
            )
        key_path_str = settings.openemr_private_key_path
        if not key_path_str:
            raise OpenEMRConfigurationError(
                "COPILOT_OPENEMR_PRIVATE_KEY_PATH is empty; run "
                "clinical-copilot/scripts/setup-openemr-client.sh to "
                "generate the RSA keypair and populate .env"
            )
        key_path = Path(key_path_str).expanduser()
        if not key_path.exists():
            raise OpenEMRConfigurationError(
                f"COPILOT_OPENEMR_PRIVATE_KEY_PATH points at {key_path!s} "
                "but no file is there. Run "
                "clinical-copilot/scripts/setup-openemr-client.sh to "
                "regenerate the keypair, or fix the path in .env."
            )
        try:
            self._private_key_pem = key_path.read_bytes()
        except OSError as exc:
            raise OpenEMRConfigurationError(
                f"could not read {key_path!s}: {exc}. The setup script "
                "stores the key with mode 0600; run as the same user "
                "that owns the file."
            ) from exc
        if not self._private_key_pem.startswith(b"-----BEGIN"):
            raise OpenEMRConfigurationError(
                f"{key_path!s} does not look like a PEM-encoded private "
                "key (no BEGIN line). Did the setup script's openssl "
                "step fail silently? Re-run setup-openemr-client.sh "
                "with --force."
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

    def _build_client_assertion(self, *, audience: str) -> str:
        """Mint a fresh jwt-bearer assertion (RFC 7523 §2.2).

        Per SMART Backend Services, the assertion claims are:
          iss/sub : the client_id
          aud     : the absolute /token endpoint URL
          exp     : now + ≤5 min (we use 4)
          jti     : a one-shot nonce so OpenEMR can detect replay
          iat     : issued-at, useful for diagnostics

        Algorithm is RS384, not RS256: OpenEMR's
        JWTClientAuthenticationService hard-codes RsaSha384Signer for
        client-assertion signature verification (RS256 assertions are
        rejected with `invalid_client / "Client authentication failed"`).
        The JWK header carries `kid` so OpenEMR's JsonWebKeySet picks
        the right key out of the registered set.
        """
        now = int(time.time())
        payload = {
            "iss": self._settings.openemr_client_id,
            "sub": self._settings.openemr_client_id,
            "aud": audience,
            "exp": now + _ASSERTION_LIFETIME_SECONDS,
            "iat": now,
            "jti": secrets.token_urlsafe(16),
        }
        # PyJWT picks the right RS384 implementation from the
        # cryptography backend (PyJWT[crypto] dependency).
        return jwt.encode(
            payload,
            self._private_key_pem,
            algorithm="RS384",
            headers={"kid": "clinical-copilot-sidecar", "typ": "JWT"},
        )

    async def _fetch_new(self) -> _CachedToken:
        endpoint = f"{self._settings.openemr_oauth_base.rstrip('/')}/token"
        scope = " ".join(SYSTEM_FHIR_SCOPES)
        try:
            assertion = self._build_client_assertion(audience=endpoint)
        except (ValueError, TypeError) as exc:
            # PyJWT raises one of these for an unparseable PEM, missing
            # cryptography backend, or wrong key format. Surface a
            # config-style error rather than a token error so the cause
            # is unambiguous in logs.
            raise OpenEMRConfigurationError(
                f"failed to sign jwt-bearer assertion with key at "
                f"{self._settings.openemr_private_key_path!s}: {exc}. "
                "Re-run clinical-copilot/scripts/setup-openemr-client.sh "
                "with --force to regenerate."
            ) from exc

        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT, verify=self._settings.fhir_verify_ssl
        ) as client:
            response = await client.post(
                endpoint,
                data={
                    "grant_type": "client_credentials",
                    "scope": scope,
                    "client_assertion_type": _JWT_BEARER_ASSERTION_TYPE,
                    "client_assertion": assertion,
                },
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
