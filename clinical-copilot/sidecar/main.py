"""Sidecar entry point.

Starts the FastAPI app on the configured port and wires observability.
"""

from __future__ import annotations

import logging

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


def create_app() -> FastAPI:
    _configure_logging()
    settings = get_settings()
    init_observability("clinical-copilot")
    app = FastAPI(
        title="Clinical Co-Pilot Sidecar",
        version="0.1.0",
        description="Pairwise comparison engine + verifier + audit log.",
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
    # OpenEMR FHIR base URL.
    def _fhir_client() -> FhirDocumentRefClient:
        if _mock_active():
            # Return a stub the handler will not actually invoke.
            from sidecar.ingest.fhir_client import StubFhirClient
            return StubFhirClient()
        # An OAuth-backed access token is acquired upstream; for the
        # initial Phase 2 wiring we use a per-request anonymous bearer.
        # Phase 3 will replace this with the OpenEMR token cache call
        # when the persistence path needs the same client.
        access_token = getattr(settings, "openemr_access_token", "") or ""
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
