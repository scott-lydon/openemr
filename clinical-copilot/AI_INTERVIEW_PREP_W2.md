# Week 2 AI Interview Prep — anticipated questions and rebuttals

> Companion to `AI_INTERVIEW_PREP.md` (Week 1). Read both before the
> case-study defense. Each question below is an anchor for a likely
> grilling probe; the answer names the specific commit, file, or
> trace attribute that proves the claim.

---

## "Why a hand-rolled supervisor instead of LangGraph's prebuilt one?"

LangGraph's `create_react_agent` and `langgraph-supervisor` hide the
routing decision behind opaque tool descriptions. The rubric demands
"every routing decision recorded as a span attribute." We get that
deterministically with a 4-rule preflight in
`sidecar/agents/w2/supervisor.py` (commit `8bd7299d8`) plus a
versioned LLM judge fallback at `supervisor.judge.v1`. The
`decision_path` lands on the span on every call; a dashboard panel
shows the fraction of decisions on the judge path so the operator
sees preflight degrading before it becomes an outage.

**Cut considered, rejected:** wiring `langgraph-supervisor` and
extracting the decision via post-hoc trace inspection. Rejected
because the post-hoc shape is fragile — a LangGraph internal change
breaks our gate. Hand-rolled is more code today, less risk over the
maintenance window.

## "Why two-pass VLM extraction? That doubles your cost."

Single-pass extractors hallucinate fields on long forms. The
two-pass design in `sidecar/agents/w2/lab_extractor.py` (commit
`541d33d89`) sends the candidates from pass 1 back to the model for
pass 2 with a verification prompt. A field rejected by pass 2 is
dropped with a `TWO_PASS_DISAGREEMENT` warning. The cost overhead
is ~one extra forward call per page; the eval suite shows the
hallucination rate drops measurably on the multi-page and
poor-quality case categories.

## "Why a Postgres queue rather than Redis or RabbitMQ?"

Three reasons: (1) we already run Postgres for pgvector, so the
queue adds no new infra dep; (2) `SELECT ... FOR UPDATE SKIP LOCKED`
gives atomic leasing without a lock service; (3) the upload handler
can write the FHIR DocumentReference + queue row in one transaction,
so a process crash mid-upload never produces an orphan. The
migration is in `20260504_0001_create_agent_jobs.py`.

**Tradeoff acknowledged:** Postgres queues do not scale past
~1000 jobs/sec on commodity hardware; Phase 12's k6 surge scenario
hits 33 jobs/sec which is well under that ceiling. If we went past
~500 jobs/sec on a real workload, Phase 12 would re-evaluate.

## "Your reranker is a SaaS. What stops a leak?"

`sidecar/rag/reranker.py` calls `assert_no_phi` BEFORE constructing
the httpx client. The guard runs five regex sweeps over the query
and every candidate document text; any match raises
`RerankerIsolationViolation` and the network call never happens. The
unit test in `test_rag.py::test_isolation_guard_catches_phi_in_query`
parametrizes over five PHI patterns and asserts every one fires.
The Hypothesis property test (Phase 2's `test_handle_upload_never_crashes_on_random_bytes`)
plus the layer-7 output guard provide the second and third lines of
defense. The contract is: any pattern match fails closed, refuses
the call, raises a typed error.

## "Why 51 cases and not 50?"

Margin. Real eval cases turn out flaky (a fixture that depended on a
network call, a model regression that lands on Tuesday). The
overshoot of 1 case keeps the rubric floor satisfied if any one case
turns out to need a fixture rebuild. The meta-test in
`evals/golden_w2/test_meta.py` enforces the >=50 floor.

## "Walk me through the citation contract."

Every clinical claim carries a `Citation` with `source_type`,
`source_id`, `page_or_section`, `field_or_chunk_id`, and
`quote_or_value` (plus optional `bbox` for document citations). The
schema's `model_validator` rejects a DocumentReference citation with
no anchor (no bbox AND empty quote) at parse time, so the verifier
never sees a citation it cannot preview. Phase 6's
`render_bbox_overlay` consumes either anchor: bbox preferred for
clean PDFs, fuzzy quote search via `page.search_for` for scanned
ones. The signed URL is HMAC-SHA256, 5-minute TTL, replayed beyond
TTL returns 401.

## "Cost ceiling — soft alert versus hard cutoff. Why both?"

Soft alert at 80% gives the operator a chance to raise the envelope
or pause the agent before service degrades. Hard cutoff at 100%
prevents one runaway loop from blowing through the daily envelope
overnight. The probe runs PER REQUEST in
`sidecar/observability/cost_ceiling.py`'s `probe_and_record`, not
post-hoc. A request that would push past the envelope is refused
WITHOUT recording its cost (so the envelope is not over-counted).
Tests cover the boundary cases including reset-on-day-rollover.

## "What's your safe-refusal contract?"

The agent refuses with a structured reason whenever:

1. Every candidate claim drops in the verifier.
2. The supervisor's preflight + judge cannot route.
3. The retriever returns zero snippets for a guideline lookup.

The refusal text is rendered verbatim in `format_response` so the
clinician knows what gap they are looking at, not "I don't know."
The eval suite has 4 missing-data probes (cases w2-041 through
w2-044) plus 2 rare-query refusal anchors (w2-033, w2-034).

## "How do you handle PHI in spans?"

Two passes: the verifier (Phase 5,
`sidecar/agents/w2/verifier.py`) scrubs claim text and the summary;
the observability gateway (Phase 10,
`sidecar/observability/phi_scrub.py`) re-scrubs every span attribute
at flush time. Production fails closed when Presidio is not
available — `PresidioRequiredButMissing` is raised at startup. The
in-process regex sweep covers SSN, MRN, DOB, phone, email, and
Patient-reference patterns and is always available as a safety net.

## "Walk me through a single trace."

A user encounter produces:

- `supervisor.decision_path`, `supervisor.intent_kind`, `supervisor.worker_sequence`, `supervisor.judge_prompt_version`.
- For the retriever: `retrieval.candidates_sparse`, `retrieval.candidates_dense`, `retrieval.fused_count`, `retrieval.reranker_used`, `retrieval.degraded`, `retrieval.query_rewrite_applied`.
- For each extracted field: `extraction.page_idx`, `extraction.page_field_count`, `extraction.page_confidence_p50`, `extraction.page_two_pass_disagreements`.
- For the verifier: `verifier.dropped_claims_count`, `verifier.phi_redactions_total`, `verifier.phi_leak_blocked`, `verifier.refused`.
- For sanitize layers: `sanitize.layer{N}.blocked` per layer.
- For cost: `cost.usd_running_total`, `cost.envelope_soft_alert_fired`.

A regression on any of these is visible in the dashboard's per-
attribute panel; the operator does not have to grep traces by hand.

## "What did you NOT cut to ship?"

- The 50-case eval (51 with margin) — every case hand-curated.
- Multi-page extraction (1, 2, 5, 12, 25 page fixtures all in scope).
- Mutation testing — runs nightly on 5 sidecar packages.
- Disaster recovery drill — `scripts/bootstrap.sh` brings a fresh
  Hetzner host to a working sidecar in under 60 minutes.
- Critic agent (Phase 11 extension) — flagged as opt-in, OFF by
  default in the demo, but the four clinical-safety rules are tested.
- Cross-vendor judge (OpenAI extracts, Anthropic Claude judges) to
  reduce shared blind spots in eval scoring.

## "What did you cut?"

- Docker image hardening (read-only fs, dropped capabilities,
  non-root) — Phase 12 documents the targets but the Hetzner deploy
  ships with the existing OpenEMR image. Adding hardening is a
  follow-up that should not block the Week 2 grade.
- Per-process Redis-backed rate limiter — Phase 12 ships the
  in-process token bucket. A multi-worker production deployment will
  swap the implementation behind the `RateLimiter` Protocol.
- LangGraph PostgresSaver checkpointer — wired in `pyproject.toml`
  but not exercised by the demo. The state is rebuilt per encounter
  and persistence beyond the encounter is a Phase 12 follow-up for a
  multi-day-conversation scenario.
