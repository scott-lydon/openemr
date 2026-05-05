# Clinical Co-Pilot Dependencies

> **Purpose:** every third-party dependency declared in `pyproject.toml` carries a one-paragraph rationale here. When you add a dependency, add the rationale at the same time. When you remove one, delete its entry.
> **Why:** auditors will ask "why this library?" The answer should be in the repo, not in someone's memory.

## Runtime base (`project.dependencies`)

- **fastapi** — Asynchronous Python web framework with first-class Pydantic integration. The OpenAPI generation falls out for free; we use it on the BFF and sidecar APIs.
- **uvicorn[standard]** — Application Server Gateway Interface (ASGI) server hosting FastAPI. The `[standard]` extra adds httptools and uvloop for lower latency.
- **httpx** — Asynchronous HTTP client. Used for OpenEMR FHIR calls and the OpenAI/Cohere clients' transport layers.
- **pydantic** — Schema validation. Every domain object passes through Pydantic; this is the keystone of the "no hallucinated fields" invariant.
- **pydantic-settings** — Typed environment configuration. Replaces ad-hoc `os.environ.get` calls with strict validation.
- **structlog** — Structured logging. Bound contexts plus PHI scrub processors fire before each emission.
- **PyYAML** — Config and prompt-template loading.
- **tenacity** — Exponential retry helpers. Used in queue worker, VLM client, FHIR client.
- **python-multipart** — Multipart form parsing for the document upload endpoint.
- **PyJWT[crypto]** — RS256 signing for SMART Backend Services jwt-bearer assertion. OpenEMR's CustomClientCredentialsGrant rejects HTTP Basic.

## OpenAI (`openai` extra)

- **openai** — Python client for OpenAI API and BAA endpoint. The VLM extractor and the LLM judges use it.
- **tiktoken** — Tokenizer for cost estimation. The runtime cost ceiling computes USD-per-call from `encode(prompt)` length.

## LangGraph (`langgraph` extra)

- **langgraph** — State graph orchestration. The supervisor + workers + verifier + formatter topology lives here.
- **langchain-openai** — Adapter for the OpenAI client inside LangGraph nodes.

## Postgres (`postgres` extra)

- **psycopg[binary]** — PostgreSQL driver. Async support, prepared statements, connection pooling.
- **pgvector** — PostgreSQL extension client. Backs the dense retrieval index with HNSW.

## Observability (`observability` extra)

- **opentelemetry-api**, **opentelemetry-sdk**, **opentelemetry-exporter-otlp** — Tracing primitives and OTLP exporter to Langfuse.
- **opentelemetry-instrumentation-fastapi**, **opentelemetry-instrumentation-httpx** — Auto-instrument FastAPI requests and HTTP client calls.
- **prometheus-client** — Metrics primitives consumed by the Grafana dashboards.

## Personal Health Information (`phi` extra)

- **presidio-analyzer** — Layer 5 of the sanitization stack. Detects PHI entities in span attributes and free-text fields before flush.
- **presidio-anonymizer** — Replaces detected PHI with typed placeholders.

## Week 2 ingestion (`w2_ingest` extra)

- **pymupdf** — PDF rendering at 300 DPI plus native text extraction for born-digital documents. Faster than pdfplumber and renders bounding boxes accurately.
- **Pillow** — Bounding-box overlay rendering for the citation preview endpoint.
- **python-magic** — MIME sniffing for the upload handler. Required because clients may spoof `Content-Type`.
- **clamd** — ClamAV daemon client over Unix socket. Layer 1 of the sanitization stack.
- **pypdf** — PDF sanitizer. Strips embedded JavaScript and external entities from uploaded PDFs before storage.

## Week 2 RAG (`w2_rag` extra)

- **cohere** — Cohere Rerank v3 client. Reranker request body is monkey-patched in tests to enforce the no-PHI invariant.
- **rank-bm25** — Sparse retrieval. Pure Python; no infrastructure cost. Used inside Reciprocal Rank Fusion (RRF).
- **tiktoken** — Already declared in the `openai` extra, listed here so `w2_rag` is independently installable.

## Week 2 sanitization (`w2_sanitize` extra)

- **llm-guard** — Layer 4 input scanning (PromptInjection, Anonymize, BanSubstrings) and Layer 7 output scanning (Sensitive, Toxicity, NoRefusal).
- **rebuff** — Second-opinion prompt-injection detector ensembled with LLM Guard at Layer 4. Reduces shared blind spots.

## Week 2 fixture rendering (`w2_render` extra)

- **reportlab** — Programmatic PDF rendering used for synthetic lab fixtures.
- **weasyprint** — HTML-to-PDF rendering used for synthetic intake forms with realistic typography.

## Week 2 judges (`w2_judges` extra)

- **anthropic** — Claude API client. Used as the cross-vendor judge: when the extractor is OpenAI, the judge is Claude. Reduces shared blind spots in pass/fail evaluation.

## Week 2 widgets (`w2_widgets` extra)

- **matplotlib** — Lab trend chart rendered server-side as PNG (Phase 11 extension).
- **rapidfuzz** — Fast fuzzy matching used by the citation preview endpoint when the VLM did not return a native bounding box.

## Week 2 testing (`w2_test` extra)

- **hypothesis** — Property-based testing. Random-input invariants on schemas and sanitization layers.
- **mutmut** — Mutation testing. Nightly run on `sidecar/agents`, `sidecar/schemas`, `sidecar/sanitize`.
- **bandit** — Static security scan in CI.
- **pip-audit** — Dependency vulnerability scan in CI.

## Development (`dev` extra)

- **pytest**, **pytest-asyncio**, **pytest-cov** — Test framework, async support, coverage reporting.
- **ruff** — Linter and formatter; replaces flake8/black/isort with one tool.
- **mypy** — Static type checker (`strict = true`).
- **respx** — HTTPX mock library used for VLM and Cohere call recording in tests.
