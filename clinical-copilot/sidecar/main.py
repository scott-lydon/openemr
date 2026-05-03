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
from sidecar.config import get_settings
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
    app.state.settings = settings
    return app


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
