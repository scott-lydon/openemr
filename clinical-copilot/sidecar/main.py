"""Sidecar entry point.

Starts the FastAPI app on the configured port and wires observability.

Background workers:

- The ingest worker runs as an asyncio task spawned in the FastAPI
  lifespan. It leases jobs from the Postgres queue, fetches the
  source PDF from the FHIR DocumentReference, runs the VLM
  extractor, and persists the extracted Observations / Medications /
  Allergies back to OpenEMR's FHIR API. See ``_start_ingest_worker``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sidecar.api.chat import router as chat_router
from sidecar.api.chat_w2 import router as chat_w2_router
from sidecar.api.citations import (
    get_citations_connection,
    get_pdf_source_for_citation,
    router as citations_router,
)
from sidecar.api.admin_licenses import router as admin_licenses_router
from sidecar.api.billing import router as billing_router
from sidecar.api.smart_launch import router as smart_launch_router
from sidecar.api.documents import (
    get_fhir_client,
    get_queue_connection,
    get_scanner,
    router as documents_router,
)
from sidecar.config import get_settings
from sidecar.ingest.fhir_client import FhirDocumentRefClient, HttpxFhirClient
from sidecar.ingest.queue import open_connection
from sidecar.ingest.virus_scan import default_scanner
from sidecar.observability import init_observability


_logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: start/stop the ingest worker background task.

    The worker runs forever leasing one job at a time. We capture its
    asyncio.Task so shutdown can cancel it cleanly. If the worker crashes
    the wrapper logs and respawns with backoff so a transient Postgres
    blip cannot wedge the queue.

    Disable at runtime by setting ``COPILOT_DISABLE_INGEST_WORKER=true``
    (set in tests where the worker would compete with fixture state, or
    on the BFF-only deployment that doesn't talk to FHIR directly).
    """
    settings = app.state.settings
    if os.environ.get("COPILOT_DISABLE_INGEST_WORKER", "").lower() == "true":
        _logger.info(
            "ingest_worker_disabled COPILOT_DISABLE_INGEST_WORKER=true; "
            "uploaded PDFs will queue but never extract."
        )
        yield
        return

    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(
        _run_ingest_worker_supervisor(settings, stop_event),
        name="ingest-worker-supervisor",
    )
    app.state.ingest_worker_task = worker_task
    app.state.ingest_worker_stop = stop_event
    try:
        yield
    finally:
        _logger.info("ingest_worker_shutdown_signal_sent")
        stop_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=15.0)
        except asyncio.TimeoutError:
            _logger.warning(
                "ingest_worker_did_not_stop_in_15s; cancelling forcibly."
            )
            worker_task.cancel()
            try:
                await worker_task
            except (asyncio.CancelledError, Exception):
                pass


async def _run_ingest_worker_supervisor(
    settings: object, stop_event: asyncio.Event
) -> None:
    """Run the ingest worker, restarting on transient errors.

    Wraps ``run_worker_loop`` so any unhandled exception is logged with
    full traceback and the loop restarts after a backoff. The supervisor
    only exits when ``stop_event`` is set, so a SIGTERM from the
    container runtime cleanly terminates both the supervisor and the
    inner worker.
    """
    backoff_seconds = 5.0
    max_backoff = 300.0
    while not stop_event.is_set():
        try:
            await _run_ingest_worker_once(settings, stop_event)
            # Clean exit means stop_event fired: leave the supervisor.
            return
        except Exception:
            # ``exc_info=True`` captures the full chain in the launcher
            # log so a "lab PDFs not appearing" report is one logfile
            # search away from the root cause.
            _logger.exception(
                "ingest_worker_crashed; restarting in %.1fs", backoff_seconds,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff_seconds)
                return  # stop_event fired during backoff
            except asyncio.TimeoutError:
                pass
            backoff_seconds = min(backoff_seconds * 2, max_backoff)


async def _run_ingest_worker_once(
    settings: object, stop_event: asyncio.Event
) -> None:
    """One run of the ingest loop. Returns when ``stop_event`` is set.

    Builds the dispatcher seams from production implementations:

    - **DocumentSource** = FHIR DocumentReference reader (token-aware).
    - **VlmClient** = OpenAIVlmClient with vision-enabled chat completions
      (or StubVlmClient when COPILOT_ALLOW_MOCK=true).
    - **FhirPersistClient** = HttpxFhirPersistClient writing transaction
      bundles back to OpenEMR.

    Token freshness:

    The clients receive a fresh OAuth2 access token per iteration. We do
    not call ``run_worker_loop`` (which freezes the token at startup)
    because SMART Backend Services tokens expire — usually in 1 hour —
    and the worker is meant to run for days. Instead we inline the loop
    so we can mint a token from ``OpenEMRTokenCache.get()`` (which
    returns the cached token until ~30s before expiry) on every lease
    attempt.
    """
    from sidecar.agents.w2.extract_dispatcher import build_extract_fn
    from sidecar.agents.w2.vlm_client import StubVlmClient
    from sidecar.ingest.errors import UploadQueueError
    from sidecar.ingest.fhir_document_source import FhirDocumentSource
    from sidecar.ingest.persist import HttpxFhirPersistClient
    from sidecar.ingest.worker import DEFAULT_POLL_SECONDS, run_one_iteration
    from sidecar.openemr_oauth import (
        OpenEMRConfigurationError,
        OpenEMRTokenCache,
        OpenEMRTokenError,
    )

    fhir_base = getattr(settings, "openemr_fhir_base", "") or ""
    verify_ssl = bool(getattr(settings, "fhir_verify_ssl", False))
    db_url = getattr(settings, "database_url", None)
    allow_mock = os.environ.get("COPILOT_ALLOW_MOCK", "").lower() == "true"

    if not fhir_base:
        raise RuntimeError(
            "ingest worker requires COPILOT_OPENEMR_FHIR_BASE to be set; "
            "without a FHIR base URL the worker cannot fetch "
            "DocumentReference bytes. Either set the env var or set "
            "COPILOT_DISABLE_INGEST_WORKER=true on a deployment that does "
            "not talk to FHIR."
        )
    if not db_url:
        raise RuntimeError(
            "ingest worker requires COPILOT_DATABASE_URL to be set; without "
            "a Postgres connection string there is no queue to lease from."
        )

    # SMART Backend Services token cache. Construction validates the
    # config (client id, private key path) and raises
    # OpenEMRConfigurationError if anything is missing — surfaced here
    # rather than on the first lease so the operator sees the problem
    # at boot.
    try:
        token_cache = OpenEMRTokenCache(settings)  # type: ignore[arg-type]
    except OpenEMRConfigurationError as exc:
        raise RuntimeError(
            "ingest worker cannot start: OpenEMR OAuth client is "
            f"misconfigured. {exc}. Either run setup-openemr-client.sh to "
            "register the sidecar with OpenEMR's oauth_clients table, or "
            "set COPILOT_DISABLE_INGEST_WORKER=true on a deployment that "
            "does not talk to FHIR."
        ) from exc

    # VLM client selection: production uses OpenAI vision; mock-mode
    # uses an empty StubVlmClient that returns a deterministic empty
    # extraction so the worker still completes the job (state -> `done`)
    # without inventing values.
    vlm_client: object
    if allow_mock:
        vlm_client = StubVlmClient(model_id="stub-vlm-mockmode")
        _logger.info(
            "ingest_worker_vlm=stub COPILOT_ALLOW_MOCK=true; extractions "
            "will be empty until a real VLM is wired."
        )
    else:
        from sidecar.agents.w2.openai_vlm_client import OpenAIVlmClient
        vlm_client = OpenAIVlmClient(settings)  # type: ignore[arg-type]

    poll_seconds = DEFAULT_POLL_SECONDS

    with open_connection(db_url) as conn:
        _logger.info(
            "ingest_worker_started fhir_base=%s allow_mock=%s "
            "poll_seconds=%.1f",
            fhir_base, allow_mock, poll_seconds,
        )
        while not stop_event.is_set():
            try:
                token = await token_cache.get()
            except OpenEMRTokenError as exc:
                _logger.warning(
                    "ingest_worker_token_mint_failed endpoint=%s status=%s "
                    "msg=%s; backing off %.1fs",
                    exc.endpoint, exc.status, exc, poll_seconds * 5,
                )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=poll_seconds * 5,
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            document_source = FhirDocumentSource(
                fhir_base=fhir_base,
                access_token=token,
                verify_ssl=verify_ssl,
            )
            persist_client = HttpxFhirPersistClient(
                fhir_base=fhir_base,
                access_token=token,
                verify_ssl=verify_ssl,
            )
            extract_fn = build_extract_fn(
                document_source=document_source,
                vlm_client=vlm_client,  # type: ignore[arg-type]
                persist_client=persist_client,
            )

            try:
                ran = await run_one_iteration(conn=conn, extract_fn=extract_fn)
            except UploadQueueError:
                _logger.warning(
                    "ingest_worker_queue_error; backing off %.1fs",
                    poll_seconds * 5,
                )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=poll_seconds * 5,
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            if not ran:
                # No work available — sleep a beat before polling again.
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=poll_seconds,
                    )
                except asyncio.TimeoutError:
                    pass


def create_app() -> FastAPI:
    _configure_logging()
    settings = get_settings()
    init_observability("clinical-copilot")
    app = FastAPI(
        title="Clinical Co-Pilot Sidecar",
        version="0.1.0",
        description="Pairwise comparison engine + verifier + audit log.",
        lifespan=_lifespan,
    )
    # CORS: the chat UI is served by this same origin (GET /), so it
    # never needs CORS for its own /chat calls. We allow the configured
    # OpenEMR origin so a future OpenEMR-side iframe can post here, and
    # nothing else — the wildcard origin is unsafe with bearer auth.
    cors_origins = getattr(settings, "cors_allowed_origins", "")
    allow_origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
    if not allow_origins:
        # Same-origin only: deny every cross-origin request by listing
        # nothing. Browsers will block; same-origin XHR is unaffected.
        allow_origins = []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(chat_router)
    app.include_router(chat_w2_router)
    app.include_router(documents_router)
    app.include_router(citations_router)
    app.include_router(billing_router)
    # admin_licenses_router enforces its own auth check (COPILOT_ADMIN_TOKEN);
    # mounting it unconditionally is safe because the endpoint refuses every
    # request when the env var is unset.
    app.include_router(admin_licenses_router)
    if os.environ.get("COPILOT_SMART_LAUNCH", "").lower() == "true":
        app.include_router(smart_launch_router)
    _wire_ingest_dependencies(app, settings)
    _wire_citations_dependencies(app, settings)
    app.state.settings = settings
    return app


def _wire_ingest_dependencies(app: FastAPI, settings: object) -> None:
    """Bind production implementations for the ingest router seams.

    Each binding is a closure so the underlying resource (database
    connection, OpenEMR token, ClamAV daemon socket) is acquired per
    request. Tests override these in pytest fixtures via
    ``app.dependency_overrides``.

    Mock-mode short-circuit: when ``COPILOT_ALLOW_MOCK=true`` every dep
    yields a no-op placeholder so the documents route can run without
    Postgres / ClamAV / OpenEMR FHIR. The route's mock branch never
    touches them; the dep just needs to resolve without raising so
    FastAPI enters the handler.
    """
    import os as _os

    def _mock_active() -> bool:
        return _os.environ.get("COPILOT_ALLOW_MOCK", "").lower() == "true"

    # Virus scanner: configured from COPILOT_VIRUS_SCAN env (defaults to
    # clamd; switch to "stub" for dev/test).
    app.dependency_overrides[get_scanner] = default_scanner

    # FHIR client: a fresh httpx client per request, mounted at the
    # OpenEMR FHIR base URL. Uses the SMART Backend Services jwt-bearer
    # flow via OpenEMRTokenCache to mint a system access token; the cache
    # holds the token until ~30s before expiry and refreshes transparently.
    #
    # We build the cache lazily on the first request because constructing
    # OpenEMRTokenCache reads files on disk (the JWT signing key) and we
    # do not want the import-time cost paid by every test fixture that
    # imports `create_app()`. The cache is also process-wide singleton-y:
    # one cache instance shared across requests so the token is reused.
    _token_cache_holder: dict[str, "OpenEMRTokenCache | None"] = {"cache": None}

    def _get_token_cache() -> "OpenEMRTokenCache":
        cached = _token_cache_holder.get("cache")
        if cached is not None:
            return cached
        # Lazy import to avoid pulling in cryptography/PyJWT at app boot
        # time when tests have already overridden _fhir_client. Failing
        # to load the key here surfaces as a 500 with the OpenEMRConfigurationError
        # message, which names the missing setting (private key path,
        # client_id, issuer, etc.) so the operator can fix .env.
        from sidecar.openemr_oauth import (
            OpenEMRConfigurationError,
            OpenEMRTokenCache,
        )
        try:
            new_cache = OpenEMRTokenCache(settings)  # type: ignore[arg-type]
        except OpenEMRConfigurationError as exc:
            raise RuntimeError(
                "OpenEMR OAuth is not configured for the upload pipeline. "
                "Set COPILOT_OPENEMR_CLIENT_ID, COPILOT_OPENEMR_PRIVATE_KEY_PATH, "
                "and COPILOT_OPENEMR_OAUTH_BASE in clinical-copilot/.env, then "
                f"restart the sidecar. Underlying: {exc}"
            ) from exc
        _token_cache_holder["cache"] = new_cache
        return new_cache

    async def _fhir_client() -> FhirDocumentRefClient:
        if _mock_active():
            # Return a stub the handler will not actually invoke.
            from sidecar.ingest.fhir_client import StubFhirClient
            return StubFhirClient()
        # Mint (or reuse) an access token via SMART Backend Services. If
        # this raises OpenEMRTokenError we let it propagate — the prior
        # behavior was to silently fall back to an empty token, which made
        # every request 500 with a confusing 'Illegal header value Bearer '
        # h11 error. Surface the real OAuth failure instead.
        from sidecar.openemr_oauth import OpenEMRTokenError
        try:
            access_token = await _get_token_cache().get()
        except OpenEMRTokenError as exc:
            raise RuntimeError(
                "Failed to mint an OpenEMR system access token for the "
                "upload pipeline. The FHIR DocumentReference write cannot "
                "proceed. Check COPILOT_OPENEMR_OAUTH_BASE reachability, "
                "the registered client's grant_types include "
                "client_credentials with the system/* scopes the upload "
                f"needs, and the JWT signing key matches the JWKS. Underlying: {exc}"
            ) from exc
        if not access_token:
            # Defensive: OpenEMRTokenCache.get() already validates non-empty,
            # but an explicit check here makes the failure obvious if someone
            # ever changes the cache's contract.
            raise RuntimeError(
                "OpenEMRTokenCache.get() returned an empty access token. "
                "The httpx client would build 'Authorization: Bearer ' with "
                "no token and h11 would reject it as an illegal header. "
                "Check the OAuth response body in the sidecar logs."
            )
        return HttpxFhirClient(
            fhir_base=getattr(settings, "openemr_fhir_base", ""),
            access_token=access_token,
            verify_ssl=getattr(settings, "fhir_verify_ssl", False),
        )

    app.dependency_overrides[get_fhir_client] = _fhir_client

    def _queue_connection():
        if _mock_active():
            # Yield a sentinel; the handler's mock branch ignores it.
            yield None
            return
        # Yield a per-request connection from the configured DATABASE_URL.
        # The contextmanager is consumed by FastAPI's dependency
        # mechanism via the generator yield protocol.
        with open_connection(getattr(settings, "database_url", None)) as conn:
            yield conn

    app.dependency_overrides[get_queue_connection] = _queue_connection


def _wire_citations_dependencies(app: FastAPI, settings: object) -> None:
    """Bind production implementations for the citations preview seams."""

    def _citations_connection():
        with open_connection(getattr(settings, "database_url", None)) as conn:
            yield conn

    app.dependency_overrides[get_citations_connection] = _citations_connection

    # PDF source resolution: try a few places in order so the preview
    # endpoint produces something useful in every demo configuration.
    #
    # 1. ``COPILOT_FIXTURE_DIR`` — when set, a document_id of the form
    #    ``mock-doc-<sha-prefix>`` is resolved by globbing the fixture
    #    directory for any PDF whose SHA-256 starts with the same
    #    prefix. Lets the demo render the actual fixture you uploaded.
    # 2. ``COPILOT_FIXTURE_DIR`` fallback: the FIRST .pdf in that dir
    #    when the prefix doesn't match. Better than 404 on demo day.
    # 3. NotImplementedError otherwise — the production wiring goes
    #    through the OpenEMR FHIR DocumentReference fetch.
    async def _pdf_source(document_id: str) -> bytes:
        import hashlib as _h
        import os as _os
        from pathlib import Path as _P

        fixture_dir = _os.environ.get("COPILOT_FIXTURE_DIR")
        if fixture_dir:
            root = _P(fixture_dir)
            if root.is_dir():
                if document_id.startswith("mock-doc-"):
                    sha_prefix = document_id[len("mock-doc-"):]
                    for pdf in sorted(root.rglob("*.pdf")):
                        try:
                            sha = _h.sha256(pdf.read_bytes()).hexdigest()
                        except OSError:
                            continue
                        if sha.startswith(sha_prefix):
                            return pdf.read_bytes()
                # Fallback to the first PDF.
                first = next(iter(sorted(root.rglob("*.pdf"))), None)
                if first is not None:
                    return first.read_bytes()
        raise NotImplementedError(
            "PDF source for citations preview is not wired. Set "
            "COPILOT_FIXTURE_DIR to a directory of synthetic PDFs for "
            "the demo, or wire the OpenEMR FHIR DocumentReference fetch "
            "in production."
        )

    app.dependency_overrides[get_pdf_source_for_citation] = lambda: _pdf_source


app = create_app()


def main() -> None:  # pragma: no cover
    settings = get_settings()
    uvicorn.run(
        "sidecar.main:app",
        host=settings.sidecar_host,
        port=settings.sidecar_port,
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
