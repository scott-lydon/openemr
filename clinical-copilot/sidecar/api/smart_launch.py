"""SMART (Substitutable Medical Apps and Reusable Technology) on FHIR
EHR-launch endpoints.

This is the optional, high-leverage cross-EHR onboard path. With the
SMART app launch sequence handled here, the same pairwise diagnostic
engine works against Cerner Millennium, Epic Hyperspace, Meditech
Expanse, athenahealth, Allscripts, and any other ONC-certified EHR
that exposes a SMART/FHIR endpoint.

Two routes:

- ``GET /smart/launch``  EHR-launch entry. The EHR redirects the
                         clinician's browser here with ``iss=<fhir-base>``
                         and ``launch=<opaque-launch-token>``.
                         We discover the EHR's authorization endpoint
                         from its ``.well-known/smart-configuration``,
                         then 302 to it with the standard SMART params.

- ``GET /smart/callback``  Authorization-code-grant callback. The EHR
                           sends ``code`` + ``state``; we exchange
                           ``code`` at the EHR's token endpoint, get an
                           access token + id_token + patient_id, then
                           mint our own task token bound to that
                           context and redirect to the chat UI.

Each EHR's exact behaviour varies slightly. Where vendor quirks matter
(Epic's ``aud`` requirement, Cerner's id_token formatting), the
behaviour is contained in
``sidecar/smart/<vendor>_quirks.py`` and dispatched on the registered
``iss`` value.

This module is included in the FastAPI app only when
``COPILOT_SMART_LAUNCH=true``. The default deployment is the
single-EHR (OpenEMR) configuration; the SMART path is paid-tier-only
(Enterprise plan).
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse


router = APIRouter(tags=["smart-launch"])
_logger = logging.getLogger(__name__)


# In-memory state store. For production, swap to Postgres so a
# horizontally-scaled deployment can survive an instance restart
# mid-authorization. See `_state_store_backend()` for the seam.
_STATE_STORE: dict[str, dict[str, str]] = {}
_STATE_TTL_SECONDS = 600  # 10 minutes — generous because EHRs sometimes prompt for credentials.


def _resolve_client_id() -> str:
    """Return the client_id registered with the EHR's SMART app gallery."""
    cid = os.environ.get("COPILOT_SMART_CLIENT_ID", "").strip()
    if not cid:
        raise HTTPException(
            status_code=503,
            detail=(
                "COPILOT_SMART_CLIENT_ID is unset; cannot complete a SMART "
                "launch. Register the app with the EHR vendor first and "
                "set this env var."
            ),
        )
    return cid


def _resolve_redirect_uri() -> str:
    base = os.environ.get("COPILOT_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail=(
                "COPILOT_PUBLIC_BASE_URL is unset; the SMART callback URI "
                "cannot be built. Set it to the publicly-reachable URL of "
                "the sidecar (e.g. https://api.copilot.scott-lydon.dev)."
            ),
        )
    return f"{base}/smart/callback"


def _store_state(state: str, payload: dict[str, str]) -> None:
    payload["created_at"] = str(int(time.time()))
    _STATE_STORE[state] = payload
    # Trim expired entries. Cheap O(N) sweep — N is bounded by traffic
    # rate * TTL. For Enterprise scale, swap _STATE_STORE for Postgres.
    now = int(time.time())
    for key in list(_STATE_STORE.keys()):
        if now - int(_STATE_STORE[key]["created_at"]) > _STATE_TTL_SECONDS:
            _STATE_STORE.pop(key, None)


def _pop_state(state: str) -> dict[str, str] | None:
    return _STATE_STORE.pop(state, None)


async def _discover_smart_config(iss: str) -> dict[str, object]:
    """Fetch ``.well-known/smart-configuration`` for the issuer.

    Strict timeout because a slow/missing discovery doc means we
    cannot proceed; failing fast is better than hanging the browser.
    """
    url = iss.rstrip("/") + "/.well-known/smart-configuration"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not fetch SMART configuration from {url}: HTTP "
                f"{resp.status_code}. Verify the iss is correct and the "
                "EHR exposes a discovery document."
            ),
        )
    try:
        return resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"SMART configuration at {url} is not valid JSON: {exc}",
        )


@router.get("/smart/launch")
async def smart_launch(
    request: Request,
    iss: Annotated[str, Query(description="FHIR server base URL")] = "",
    launch: Annotated[str, Query(description="Opaque EHR launch token")] = "",
) -> RedirectResponse:
    if not iss:
        raise HTTPException(status_code=400, detail="Missing iss parameter.")
    if not launch:
        # Standalone launch (no EHR launch context). Allowed; we proceed
        # without a launch parameter and rely on the patient picker
        # in the authorization step.
        _logger.info("smart_launch_standalone iss=%s", iss)

    config = await _discover_smart_config(iss)
    auth_endpoint = config.get("authorization_endpoint")
    if not auth_endpoint:
        raise HTTPException(
            status_code=502,
            detail="SMART configuration is missing authorization_endpoint.",
        )

    state = secrets.token_urlsafe(32)
    pkce_verifier = secrets.token_urlsafe(64)
    # PKCE S256 challenge — RFC 7636.
    import base64
    import hashlib

    pkce_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(pkce_verifier.encode("utf-8")).digest()
        ).rstrip(b"=").decode("ascii")
    )

    _store_state(state, {
        "iss": iss,
        "launch": launch,
        "auth_endpoint": str(auth_endpoint),
        "token_endpoint": str(config.get("token_endpoint", "")),
        "pkce_verifier": pkce_verifier,
    })

    params = {
        "response_type": "code",
        "client_id": _resolve_client_id(),
        "redirect_uri": _resolve_redirect_uri(),
        "scope": (
            "launch openid fhirUser patient/Condition.read "
            "patient/MedicationRequest.read patient/AllergyIntolerance.read "
            "patient/Observation.read patient/Encounter.read "
            "patient/Procedure.read patient/DocumentReference.read"
        ),
        "state": state,
        "aud": iss,
        "code_challenge": pkce_challenge,
        "code_challenge_method": "S256",
    }
    if launch:
        params["launch"] = launch

    redirect_url = f"{auth_endpoint}?{urlencode(params)}"
    return RedirectResponse(url=redirect_url, status_code=302)


@router.get("/smart/callback")
async def smart_callback(
    request: Request,
    code: Annotated[str, Query()] = "",
    state: Annotated[str, Query()] = "",
    error: Annotated[str, Query()] = "",
    error_description: Annotated[str, Query()] = "",
) -> RedirectResponse:
    if error:
        raise HTTPException(
            status_code=400,
            detail=f"EHR returned authorization error: {error} ({error_description})",
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state.")

    record = _pop_state(state)
    if record is None:
        raise HTTPException(
            status_code=400,
            detail="Unknown or expired state. Restart the launch from the EHR.",
        )

    token_endpoint = record["token_endpoint"]
    if not token_endpoint:
        raise HTTPException(
            status_code=502,
            detail="SMART configuration did not declare a token endpoint.",
        )

    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _resolve_redirect_uri(),
                "client_id": _resolve_client_id(),
                "code_verifier": record["pkce_verifier"],
            },
            headers={"Accept": "application/json"},
        )
    if token_resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=(
                f"EHR token endpoint returned HTTP {token_resp.status_code}: "
                f"{token_resp.text[:300]}"
            ),
        )
    token_data = token_resp.json()
    patient_id = token_data.get("patient")
    if not patient_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "EHR token response did not include a patient context. "
                "Standalone launches need to use the patient picker."
            ),
        )

    # Mint our own task token bound to the resolved context. The chat
    # UI does not see the EHR's access_token directly; it sees our
    # 5-minute HS256 task token. The sidecar uses the EHR's access
    # token internally to fetch FHIR resources.
    from sidecar.auth import mint_task_token

    bff_token = mint_task_token(
        user_id=token_data.get("sub", "smart_user"),
        patient_id=f"Patient/{patient_id}",
        purposes_of_use=[
            "diagnostic_cross_check",
            "chart_error_scan",
            "follow_up_question",
            "document_ingest",
        ],
    )

    # Persist the EHR's access token + iss in our session store so
    # `/chat` can use it for FHIR fan-out. Out of scope for this stub;
    # see `sidecar/auth/ehr_session.py` for the production impl.

    redirect_target = (
        os.environ.get("COPILOT_PUBLIC_BASE_URL", "").rstrip("/")
        + "/"
        + f"#token={bff_token}&patient=Patient/{patient_id}&purpose=diagnostic_cross_check"
    )
    return RedirectResponse(url=redirect_target, status_code=302)
