# Clinical Co-Pilot sidecar — production image.
# Multi-stage build: install deps in the builder, ship a slim runtime.
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml ./
COPY sidecar ./sidecar
COPY bff ./bff
RUN pip install --upgrade pip wheel \
    && pip install ".[openai,langgraph,postgres,observability,phi,w2_ingest]"
# Note: w2_ingest is required by the /agent-api/v1/patients/{pid}/documents
# upload pipeline (pypdf, pymupdf, python-magic, clamd). Without it every
# PDF upload 500s with `ModuleNotFoundError: No module named 'pypdf'`,
# which is the exact regression that hit prod on 2026-05-08.
#
# w2_sanitize and w2_render are NOT installed here on purpose:
#   - sidecar.sanitize is referenced only from tests, never from a
#     runtime import path, so llm-guard / rebuff are dev-only deps.
#   - reportlab / weasyprint are fixture-rendering helpers, not
#     production code paths.

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONHASHSEED=random
# Native shared libraries that the w2_ingest pipeline links to at import
# time (NOT pip-installable):
#   - libmagic1: backing C library for python-magic. `import magic` raises
#     ImportError("failed to find libmagic. Check your installation") on
#     the slim base without it, which 500s every upload before any code
#     gets a chance to inspect the bytes.
# Keep this list minimal so the runtime image stays small.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libmagic1 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --create-home --shell /bin/bash copilot
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY sidecar ./sidecar
COPY bff ./bff
COPY ui ./ui
COPY fixtures ./fixtures
USER copilot
EXPOSE 8801
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8801/health', timeout=2)" || exit 1
CMD ["uvicorn", "sidecar.main:app", "--host", "0.0.0.0", "--port", "8801"]
