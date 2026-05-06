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
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from sidecar.agent.conversation import (
    ConversationMemory,
    ConversationTurn,
    get_default_memory,
)
from sidecar.agent.follow_up import FollowUpConfig, run_follow_up
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
    Presenting,
    SnapshotService,
    build_snapshot_from_fixture,
)
from sidecar.snapshot.fhir_client import FhirClient
from sidecar.snapshot.shard_selection import ShardSelection, select_shards

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


async def _snapshot_from_openemr(
    patient_id: str, *, shards: ShardSelection | None = None
) -> PatientSnapshot:
    """Fetch a snapshot from OpenEMR's FHIR R4 surface.

    ``shards`` controls which FHIR resource shards are pulled. When
    ``None``, the legacy default fan-out (every shard) is used. The
    chat handler computes the right selection per request and passes
    it through.
    """
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
        return await SnapshotService(client).build(uuid, shards=shards)


async def _snapshot_for(
    patient_id: str,
    *,
    claims: TaskTokenClaims,
    shards: ShardSelection | None = None,
) -> PatientSnapshot:
    """Resolve a snapshot, enforcing patient-id binding from the token.

    Production path: call OpenEMR FHIR via :func:`_snapshot_from_openemr`.
    Dev/CI/eval path (``COPILOT_ALLOW_MOCK=true``): fall back to the
    bundled fixture if one matches the requested patient id, else still
    call OpenEMR. Either way, the requested patient id MUST equal the
    patient id bound into the task token.

    ``shards`` is honoured on the live OpenEMR path. The fixture path
    ignores it because the bundled JSON already contains every
    shard's worth of data (selective retrieval is a network-cost
    optimisation, not a privacy boundary).
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
    return await _snapshot_from_openemr(patient_id, shards=shards)


class ChatRequest(BaseModel):
    patient_id: str = Field(description="FHIR Patient/{uuid} resource id")
    purpose: Literal["diagnostic_cross_check", "chart_error_scan", "follow_up_question"]
    message: str | None = Field(
        default=None,
        description=(
            "Clinician's message text. Required for purpose='follow_up_question'. "
            "For 'diagnostic_cross_check' and 'chart_error_scan' the message is "
            "optional; if provided, it is treated as a presenting-symptom "
            "override and drives the pairwise comparator's prompt."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Conversation session id. Multiple turns sharing the same "
            "(user_id, patient_id, session_id) inherit prior turns' "
            "messages as context. The BFF mints this on launch; "
            "follow-up callers should reuse it across turns. The chat "
            "endpoint synthesises a stable id when missing so a "
            "single-turn caller still works."
        ),
    )


class ChatResponse(BaseModel):
    verdict: str
    candidates: list[dict] = Field(default_factory=list)
    chart_error_flags: list[dict] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    telemetry: dict = Field(default_factory=dict)
    session_id: str | None = Field(
        default=None,
        description=(
            "Echoed back to the caller — useful when the request omitted "
            "session_id and the server synthesised one. Reuse this on "
            "subsequent turns to thread the conversation."
        ),
    )


def _resolve_session_id(body_session_id: str | None, claims: TaskTokenClaims) -> str:
    """Return the session id to thread state under.

    Precedence:

    1. The caller's explicit ``session_id``.
    2. A claim-derived stable id ``"sid:<user>:<patient>"``. Stable
       across reconnects but unique per (clinician, patient), so a
       single-page UI that forgets to mint a session still gets
       multi-turn memory within one chart open.

    Empty strings are treated as "not provided" because Pydantic's
    ``str | None`` does not auto-coerce ``""`` to ``None``.
    """
    if body_session_id and body_session_id.strip():
        return body_session_id.strip()
    return f"sid:{claims.user_id}:{claims.patient_id}"


def _presenting_for_message(message: str | None) -> Presenting | None:
    """Inject the clinician's message as a presenting symptom.

    Used for the ``diagnostic_cross_check`` and ``chart_error_scan``
    purposes when the caller supplied a non-empty message: the
    pairwise comparator's prompts read ``snapshot.presenting.symptoms``
    directly, so writing the message there is the smallest wedge that
    actually drives the model input. Returns ``None`` when there is
    nothing to inject so the snapshot's reconciler-derived presenting
    section remains untouched.
    """
    if not message or not message.strip():
        return None
    text = message.strip()
    return Presenting(
        symptoms=[text],
        since=None,
        source="chat_message_override",
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    claims: TaskTokenClaims = Depends(require_task_token),
    mock: int = 0,
    memory: ConversationMemory = Depends(get_default_memory),
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

    Selective retrieval. The chat handler computes a per-request
    :class:`ShardSelection` from ``body.purpose`` and ``body.message``
    (see :mod:`sidecar.snapshot.shard_selection`) and passes it to the
    snapshot fetch — the fan-out only pulls the FHIR shards a given
    turn actually needs.

    Session memory. When ``body.message`` is present the turn is
    appended to a process-local ``ConversationMemory`` keyed by
    ``(user_id, patient_id, session_id)``. ``follow_up_question``
    turns prepend prior messages to the model prompt so questions
    like "what about her CRP?" inherit context from the previous turn.
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
    if body.purpose == "follow_up_question" and (
        body.message is None or not body.message.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_message",
                "message": (
                    "purpose='follow_up_question' requires a non-empty "
                    "'message' field. Without a message there is "
                    "nothing for the model to answer."
                ),
            },
        )

    from sidecar.agent.graph import make_provider

    settings = get_settings()
    force_mock = bool(mock) and getattr(settings, "allow_mock", False)
    if mock and not force_mock:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "mock_not_allowed",
                "message": "?mock=1 requires COPILOT_ALLOW_MOCK=true",
            },
        )

    # Compute the shard selection once. Errors here mean a typo in a
    # purpose value got past Pydantic — re-raise as 500 because that
    # is a server-side bug, not a client-side input error.
    try:
        shards = select_shards(body.purpose, body.message)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "shard_selection_failed",
                "message": str(exc),
            },
        ) from exc

    snapshot = await _snapshot_for(body.patient_id, claims=claims, shards=shards)
    if body.purpose != "follow_up_question":
        # Inject the clinician's message text into the snapshot's
        # presenting block so the pairwise comparator's prompts
        # actually carry the user's input. Without this the message
        # field was inert.
        injected = _presenting_for_message(body.message)
        if injected is not None:
            snapshot = snapshot.model_copy(update={"presenting": injected})

    session_id = _resolve_session_id(body.session_id, claims)
    conv_memory = memory or get_default_memory()

    if body.purpose == "follow_up_question":
        prior_turns = conv_memory.turns(
            user_id=claims.user_id,
            patient_id=body.patient_id,
            session_id=session_id,
        )
        cfg = FollowUpConfig(
            message=body.message or "",
            prior_turns=prior_turns,
            user_id=claims.user_id,
            settings=settings,
            audit_log=_AUDIT_LOG,
            provider=make_provider(settings, force_mock=force_mock),
        )
        response = await run_follow_up(snapshot, cfg)
    else:
        cfg = GraphConfig(
            purpose=body.purpose,
            user_id=claims.user_id,
            settings=settings,
            audit_log=_AUDIT_LOG,
            provider=make_provider(settings, force_mock=force_mock),
        )
        response = await run_graph(snapshot, cfg)

    # Record this turn for any future follow-ups in the session.
    if body.message and body.message.strip():
        now = time.time()
        conv_memory.record(
            user_id=claims.user_id,
            patient_id=body.patient_id,
            session_id=session_id,
            turn=ConversationTurn(
                role="user", content=body.message.strip(),
                ts_unix=now, purpose=body.purpose,
            ),
        )
        # Store a redacted assistant turn — the verdict + at most
        # one short headline. Free-text answers, candidate labels,
        # and chart quotes never go in.
        assistant_summary = response.verdict
        headline = ""
        if response.candidates:
            first = response.candidates[0]
            label = str(first.get("label") or "")
            if label and label != "follow_up_answer":
                headline = label[:80]
        if headline:
            assistant_summary = f"{response.verdict}: {headline}"
        conv_memory.record(
            user_id=claims.user_id,
            patient_id=body.patient_id,
            session_id=session_id,
            turn=ConversationTurn(
                role="assistant", content=assistant_summary,
                ts_unix=now, purpose=body.purpose,
            ),
        )

    logger.info(
        "chat_handled",
        extra={
            "patient_id": body.patient_id,
            "purpose": body.purpose,
            "verdict": response.verdict,
            "pair_count": response.telemetry.get("total_pair_count"),
            "session_id": session_id,
            "shard_count": len(shards),
            "shards": sorted(shards.names),
            "message_chars": len(body.message or ""),
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
        telemetry={
            **response.telemetry,
            "shards_pulled": sorted(shards.names),
            "prior_turn_count": len(
                conv_memory.turns(
                    user_id=claims.user_id,
                    patient_id=body.patient_id,
                    session_id=session_id,
                )
            ),
        },
        session_id=session_id,
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


@router.get("/diagnostic")
def diagnostic() -> dict[str, object]:
    """Self-describing config dump for the running sidecar.

    Unauthenticated by design. Returns:

    - ``version.git_hash`` — the commit SHA the running interpreter is
      executing. Lets the operator verify they have not been chasing a
      ghost across a stale .venv (the failure mode that wasted hours
      before this endpoint existed).
    - ``version.python`` — for sanity when bug reports come in.
    - ``version.openemr_oauth_module`` — the absolute filesystem path
      of the imported sidecar.openemr_oauth module, so an editable
      install pointing at a stale source tree is immediately visible.
    - ``config`` — sanitised effective settings: client_id is
      truncated, secrets are reported as a boolean ``set/unset``, URLs
      and paths are returned in full because they are not secret.
    - ``checks`` — per-feature self-tests: private key file presence
      and mode, OpenEMR token endpoint reachability, and which
      authentication method the running code path uses.
    - ``auth_method`` is the authoritative answer to "is the new
      jwt-bearer code actually loaded": it reads the
      ``OpenEMRTokenCache`` constructor's behaviour, not a constant.

    Operators (and the setup script) should hit this endpoint after
    every sidecar restart. The setup script refuses to declare success
    unless ``version.git_hash`` matches the working-tree HEAD.
    """
    import sys
    import subprocess

    settings = get_settings()

    # Git hash of the source tree the running Python is executing.
    src_root = Path(__file__).resolve().parent.parent.parent
    git_hash = "unknown"
    try:
        proc = subprocess.run(
            ["git", "-C", str(src_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if proc.returncode == 0:
            git_hash = proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass

    # Path the OpenEMR oauth module was actually imported from. If
    # editable install is broken this points at a site-packages copy.
    openemr_oauth_module_path = "unknown"
    try:
        from sidecar import openemr_oauth as _oo
        openemr_oauth_module_path = getattr(_oo, "__file__", "unknown") or "unknown"
    except Exception as exc:  # noqa: BLE001
        openemr_oauth_module_path = f"import failed: {exc}"

    # Auth method: detect whether the running code uses jwt-bearer
    # (new) or HTTP Basic + client_secret (old). We check by looking
    # for a method that only exists on the rewritten class.
    auth_method = "unknown"
    try:
        from sidecar.openemr_oauth import OpenEMRTokenCache as _Cache
        if hasattr(_Cache, "_build_client_assertion"):
            auth_method = "private_key_jwt"
        elif "client_secret" in _Cache.__init__.__doc__ or "":
            auth_method = "http_basic_legacy"
        else:
            auth_method = "unknown_legacy"
    except Exception as exc:  # noqa: BLE001
        auth_method = f"import failed: {exc}"

    # Private key file health.
    key_path = getattr(settings, "openemr_private_key_path", None)
    key_status: dict[str, object]
    if not key_path:
        key_status = {"path": None, "present": False, "reason": "COPILOT_OPENEMR_PRIVATE_KEY_PATH unset"}
    else:
        p = Path(key_path)
        if not p.exists():
            key_status = {"path": str(p), "present": False, "reason": "no file at path"}
        else:
            try:
                st = p.stat()
                key_status = {
                    "path": str(p),
                    "present": True,
                    "size_bytes": st.st_size,
                    "mode": oct(st.st_mode & 0o777),
                    "starts_with_pem_header": p.read_bytes()[:11] == b"-----BEGIN ",
                }
            except OSError as exc:
                key_status = {"path": str(p), "present": True, "reason": f"unreadable: {exc}"}

    # Multi-purpose claim support: detect whether the chat handler
    # checks membership (new) or strict equality (old).
    purpose_check = "unknown"
    try:
        from sidecar.auth import TaskTokenClaims as _Claims
        # Dataclass fields live on __dataclass_fields__, not as class
        # attributes, so hasattr() against the class is misleading.
        fields = _Claims.__dataclass_fields__
        if "authorized_purposes" in fields and hasattr(_Claims, "is_purpose_authorized"):
            purpose_check = "membership_in_authorized_purposes"
        elif "purpose_of_use" in fields:
            purpose_check = "strict_equality_legacy"
        else:
            purpose_check = "unknown_legacy"
    except Exception as exc:  # noqa: BLE001
        purpose_check = f"import failed: {exc}"

    return {
        "version": {
            "git_hash": git_hash,
            "python": sys.version.split()[0],
            "openemr_oauth_module": openemr_oauth_module_path,
        },
        "config": {
            "openemr_client_id_prefix": (
                settings.openemr_client_id[:12] + "…"
                if settings.openemr_client_id else None
            ),
            "openemr_client_id_set": bool(settings.openemr_client_id),
            "openemr_oauth_base": settings.openemr_oauth_base,
            "openemr_fhir_base": settings.openemr_fhir_base,
            "openemr_private_key_path": key_path,
            "openemr_client_secret_set": bool(getattr(settings, "openemr_client_secret", None)),
            "fhir_verify_ssl": settings.fhir_verify_ssl,
            "allow_mock": getattr(settings, "allow_mock", False),
            "bff_jwt_signing_key_set": (
                bool(settings.bff_jwt_signing_key)
                and settings.bff_jwt_signing_key != "change-me-to-a-32-byte-hex-string"
            ),
        },
        "checks": {
            "auth_method": auth_method,
            "task_token_purpose_check": purpose_check,
            "private_key_file": key_status,
        },
    }


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
