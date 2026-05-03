"""HTTP routes: chat + health.

The sidecar speaks JSON over HTTP. Production traffic flows
``OpenEMR → BFF → sidecar`` with a 5-minute task token (HS256 JWT) the
BFF mints (``ARCHITECTURE.md`` §1.1, §3.2).

Auth: every state-changing or PHI-touching route depends on
:func:`sidecar.auth.require_task_token`. The dependency rejects requests
without a valid Bearer token and exposes the verified claims to the
handler so per-patient binding can be enforced.

Data source: snapshots come from OpenEMR's FHIR R4 surface via the
in-process :class:`SnapshotService` + :class:`FhirClient`. The bundled
JSON fixtures in ``fixtures/patients/`` are still loadable, but ONLY when
``COPILOT_ALLOW_MOCK=true`` (set in dev/CI/eval). The OpenEMR launch
button in production never sets allow_mock, so a misconfigured deploy
fails closed instead of silently serving synthetic data.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from sidecar.agent.graph import GraphConfig, run_graph
from sidecar.audit import InMemoryAuditLog
from sidecar.auth import TaskTokenClaims, require_task_token
from sidecar.config import get_settings
from sidecar.openemr_oauth import (
    OpenEMRConfigurationError,
    OpenEMRTokenCache,
    OpenEMRTokenError,
)
from sidecar.snapshot import (
    PatientSnapshot,
    SnapshotService,
    build_snapshot_from_fixture,
)
from sidecar.snapshot.fhir_client import FhirClient

logger = logging.getLogger(__name__)

router = APIRouter()

# Singleton audit log per process. Production swaps in PostgresAuditLog.
_AUDIT_LOG = InMemoryAuditLog()

# Bundled fixtures, loadable ONLY when allow_mock=true (dev / CI / eval).
_FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "patients"
_FIXTURE_BY_PATIENT_ID: dict[str, Path] = {}

# Process-wide OpenEMR token cache. Created lazily on first FHIR call so
# the sidecar still starts when OpenEMR config is missing in mock mode.
_TOKEN_CACHE: OpenEMRTokenCache | None = None


def _load_fixtures() -> None:
    """Load bundled JSON fixtures into the in-memory map (dev only)."""
    if _FIXTURE_BY_PATIENT_ID:
        return
    if not _FIXTURE_DIR.exists():
        return
    for path in _FIXTURE_DIR.glob("*.json"):
        try:
            snapshot = build_snapshot_from_fixture(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fixture_load_failed", extra={"path": str(path), "err": str(exc)})
            continue
        _FIXTURE_BY_PATIENT_ID[snapshot.patient_id] = path


def _get_token_cache() -> OpenEMRTokenCache:
    """Return the process-wide token cache, creating it on first call."""
    global _TOKEN_CACHE
    if _TOKEN_CACHE is None:
        try:
            _TOKEN_CACHE = OpenEMRTokenCache(get_settings())
        except OpenEMRConfigurationError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "openemr_oauth_unconfigured",
                    "message": str(exc),
                },
            ) from exc
    return _TOKEN_CACHE


async def _snapshot_from_openemr(patient_id: str) -> PatientSnapshot:
    """Fetch a snapshot from OpenEMR's FHIR R4 surface."""
    settings = get_settings()
    cache = _get_token_cache()
    try:
        token = await cache.get()
    except OpenEMRTokenError as exc:
        logger.error(
            "openemr_token_failed",
            extra={"endpoint": exc.endpoint, "status": exc.status},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "openemr_oauth_failed",
                "message": str(exc),
            },
        ) from exc
    uuid = patient_id.removeprefix("Patient/")
    if not uuid or "/" in uuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "malformed_patient_id",
                "message": f"expected 'Patient/<uuid>', got {patient_id!r}",
            },
        )
    async with FhirClient(settings, token) as client:
        return await SnapshotService(client).build(uuid)


async def _snapshot_for(patient_id: str, *, claims: TaskTokenClaims) -> PatientSnapshot:
    """Resolve a snapshot, enforcing patient-id binding from the token.

    Production path: call OpenEMR FHIR via :func:`_snapshot_from_openemr`.
    Dev/CI/eval path (``COPILOT_ALLOW_MOCK=true``): fall back to the
    bundled fixture if one matches the requested patient id, else still
    call OpenEMR. Either way, the requested patient id MUST equal the
    patient id bound into the task token.
    """
    if patient_id != claims.patient_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "patient_id_mismatch",
                "message": (
                    f"task token authorises {claims.patient_id!r}, "
                    f"but request asked for {patient_id!r}"
                ),
            },
        )
    settings = get_settings()
    if getattr(settings, "allow_mock", False):
        _load_fixtures()
        if patient_id in _FIXTURE_BY_PATIENT_ID:
            return build_snapshot_from_fixture(_FIXTURE_BY_PATIENT_ID[patient_id])
    return await _snapshot_from_openemr(patient_id)


class ChatRequest(BaseModel):
    patient_id: str = Field(description="FHIR Patient/{uuid} resource id")
    purpose: Literal["diagnostic_cross_check", "chart_error_scan", "follow_up_question"]
    message: str | None = Field(
        default=None,
        description="Optional follow-up message text. The first turn for "
        "diagnostic_cross_check or chart_error_scan does not need a message.",
    )


class ChatResponse(BaseModel):
    verdict: str
    candidates: list[dict] = Field(default_factory=list)
    chart_error_flags: list[dict] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    telemetry: dict = Field(default_factory=dict)


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    claims: TaskTokenClaims = Depends(require_task_token),
    mock: int = 0,
) -> ChatResponse:
    """Handle one turn of the conversation.

    Requires a BFF-minted task token in the ``Authorization`` header.
    The token's ``patient_id`` claim must equal ``body.patient_id``;
    that constraint is enforced by :func:`_snapshot_for`. ``body.purpose``
    must be a member of the token's ``authorized_purposes`` set
    (the JSON array stored in the ``purpose_of_use`` claim) — the UI
    fans out one ``/chat`` call per purpose, and the launch endpoint
    binds the token to every purpose the UI will exercise.

    The audit log still records the per-call ``body.purpose``, so the
    breadth of authorisation and the actually-exercised purpose remain
    distinguishable downstream.

    ``?mock=1`` forces the deterministic mock provider, but is rejected
    unless ``COPILOT_ALLOW_MOCK=true``.
    """
    if not claims.is_purpose_authorized(body.purpose):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "purpose_of_use_not_authorized",
                "message": (
                    f"task token authorises purposes="
                    f"{list(claims.authorized_purposes)!r}, "
                    f"request asked for purpose={body.purpose!r}"
                ),
            },
        )
    from sidecar.agent.graph import make_provider

    settings = get_settings()
    snapshot = await _snapshot_for(body.patient_id, claims=claims)
    force_mock = bool(mock) and getattr(settings, "allow_mock", False)
    if mock and not force_mock:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "mock_not_allowed",
                "message": "?mock=1 requires COPILOT_ALLOW_MOCK=true",
            },
        )
    cfg = GraphConfig(
        purpose=body.purpose,
        user_id=claims.user_id,
        settings=settings,
        audit_log=_AUDIT_LOG,
        provider=make_provider(settings, force_mock=force_mock),
    )
    response = await run_graph(snapshot, cfg)
    logger.info(
        "chat_handled",
        extra={
            "patient_id": body.patient_id,
            "purpose": body.purpose,
            "verdict": response.verdict,
            "pair_count": response.telemetry.get("total_pair_count"),
            "user_id": claims.user_id,
            "mock": force_mock,
        },
    )
    return ChatResponse(
        verdict=response.verdict,
        candidates=response.candidates,
        chart_error_flags=response.chart_error_flags,
        data_gaps=response.data_gaps,
        dropped=response.dropped,
        telemetry=response.telemetry,
    )


@router.get("/snapshot/{patient_uuid}")
async def get_snapshot(
    patient_uuid: str,
    claims: TaskTokenClaims = Depends(require_task_token),
) -> dict:
    """Return the deterministically-reconciled patient snapshot as JSON.

    Requires a task token whose ``patient_id`` claim equals
    ``Patient/{patient_uuid}``.
    """
    snapshot = await _snapshot_for(f"Patient/{patient_uuid}", claims=claims)
    return snapshot.model_dump(mode="json")


@router.get("/audit/head")
def audit_head(
    _claims: TaskTokenClaims = Depends(require_task_token),
) -> dict[str, str | int]:
    """Return the latest audit chain head and length."""
    head = _AUDIT_LOG.head_hash().hex()
    length = sum(1 for _ in _AUDIT_LOG)
    chain_ok = _AUDIT_LOG.verify_chain()
    return {"head_hash": head, "length": length, "chain_intact": chain_ok}


@router.get("/audit/list")
def audit_list(
    limit: int = 50,
    _claims: TaskTokenClaims = Depends(require_task_token),
) -> list[dict]:
    """Return the most recent audit entries (newest first)."""
    entries = list(_AUDIT_LOG)
    entries.reverse()
    out: list[dict] = []
    for e in entries[:limit]:
        out.append({
            "ts": e.ts.isoformat() if hasattr(e.ts, "isoformat") else str(e.ts),
            "patient_id": e.patient_id,
            "user_id": e.user_id,
            "purpose": e.purpose,
            "verdict": e.verdict,
            "prompt_fingerprint": e.prompt_fingerprint,
            "summary": e.redacted_summary,
            "telemetry": e.telemetry,
            "row_hash": e.row_hash.hex() if isinstance(e.row_hash, (bytes, bytearray)) else str(e.row_hash),
            "prev_hash": e.prev_hash.hex() if isinstance(e.prev_hash, (bytes, bytearray)) else str(e.prev_hash),
        })
    return out


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe — unauthenticated by design."""
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
def root_ui() -> HTMLResponse:
    """Serve the embedded chat UI.

    The UI itself is unauthenticated HTML; it reads the task token from
    the URL fragment (``#token=...``) and includes it in every API call.
    """
    ui_path = Path(__file__).resolve().parent.parent.parent / "ui" / "chat.html"
    if not ui_path.exists():
        return HTMLResponse("<html><body><p>UI not bundled.</p></body></html>")
    return HTMLResponse(ui_path.read_text(encoding="utf-8"))


@router.get("/demo", response_class=HTMLResponse)
@router.get("/demo/", response_class=HTMLResponse)
def demo_ui() -> HTMLResponse:
    """Serve the chat UI in deterministic-mock mode for demo recordings."""
    settings = get_settings()
    if not getattr(settings, "allow_mock", False):
        return HTMLResponse(
            "<html><body style='font-family:system-ui;max-width:540px;margin:48px auto;'>"
            "<h2>/demo is disabled</h2>"
            "<p>Set <code>COPILOT_ALLOW_MOCK=true</code> in your <code>.env</code> "
            "and restart the sidecar to enable the demo route.</p>"
            "</body></html>",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    ui_path = Path(__file__).resolve().parent.parent.parent / "ui" / "chat.html"
    if not ui_path.exists():
        return HTMLResponse("<html><body><p>UI not bundled.</p></body></html>")
    html = ui_path.read_text(encoding="utf-8")
    inject = (
        "<script>window.__COPILOT_DEMO__ = true;</script>"
        "<style>header.app::after{content:'DEMO (deterministic mock data)';"
        "background:#fde68a;color:#92400e;padding:3px 10px;border-radius:99px;"
        "font-size:11px;font-weight:700;margin-left:auto;letter-spacing:.04em;}</style>"
    )
    html = html.replace("</head>", inject + "</head>", 1)
    return HTMLResponse(html)


@router.get("/patients", response_model=list[str])
def list_known_patients() -> list[str]:
    """Demo helper: which fixtures are available (mock mode only)."""
    settings = get_settings()
    if not getattr(settings, "allow_mock", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "mock_not_allowed",
                "message": "fixture listing only available with COPILOT_ALLOW_MOCK=true",
            },
        )
    _load_fixtures()
    return sorted(_FIXTURE_BY_PATIENT_ID.keys())
