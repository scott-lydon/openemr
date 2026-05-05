# Clinical Co-Pilot Runbook

> **Audience:** on-call engineer or maintainer hitting a production failure.
> **Update policy:** every named failure mode in `W2_QUALITY_PLAN.md` adds an entry. Each entry names the symptom, the trace attribute or log signature that proves the diagnosis, and the fix command.

---

## How to use this runbook

1. Pull the trace ID from the user-facing error or from the alert.
2. Look up the trace in the observability backend; note the failing span and the relevant attribute.
3. Find the matching entry below; follow the fix command verbatim.
4. If the symptom is not listed, add it after resolving so the next incident is faster.

---

## Phase 1 — Environment

### 1.1 `scripts/check_environment.sh` reports a missing dependency

**Symptom:** the bootstrap script prints `MISSING <name>` for one or more dependencies.

**Diagnosis:** the dependency is not installed on the host.

**Fix:**

| Dependency | Install on macOS | Install on Linux |
|---|---|---|
| ClamAV daemon | `brew install clamav && brew services start clamav` | `apt-get install -y clamav clamav-daemon && systemctl start clamav-daemon` |
| ImageMagick | `brew install imagemagick` | `apt-get install -y imagemagick` |
| `libmagic` for `python-magic` | `brew install libmagic` | `apt-get install -y libmagic1` |
| Postgres `pgvector` extension | `brew install pgvector` then `CREATE EXTENSION pgvector;` in your database | `apt-get install -y postgresql-15-pgvector` then `CREATE EXTENSION pgvector;` |
| Presidio model bundle | `python -m spacy download en_core_web_lg` | same |

After install, rerun `bash scripts/check_environment.sh`. Every line should end with `OK`.

---

## Phase 2 — Document upload and queue

### 2.1 Queue depth growing

**Symptom:** `agent_jobs` table has a growing number of rows in state `queued`. Grafana panel "Queue depth" climbs.

**Diagnosis:** the queue worker process is not running, or it is blocked on an external service (Vision Language Model endpoint, Cohere reranker).

**Fix:**

```bash
# Check whether the worker is running.
docker compose ps sidecar-worker
# Tail the worker logs.
docker compose logs --tail=200 sidecar-worker
# If the worker is up but blocked on the VLM endpoint, the logs will show
# repeated 429 or 5xx from OpenAI. Lower the worker concurrency until the
# rate limit resolves.
```

If the queue is over 1000 jobs, scale horizontally:

```bash
docker compose up -d --scale sidecar-worker=4
```

### 2.2 Dead letter count rising

**Symptom:** `agent_jobs.state='dead_letter'` row count increments. Grafana panel "Dead letter count" climbs. PagerDuty alert fires at 10 per hour.

**Diagnosis:** a permanent failure (malformed schema, persistent VLM error, Pydantic validation rejection) is sending jobs to dead letter after `max_attempts`.

**Fix:**

1. Pull a sample dead-letter job and inspect `last_error`:

```bash
docker compose exec postgres psql -U sidecar -d sidecar -c \
  "SELECT job_id, document_id, last_error FROM agent_jobs WHERE state='dead_letter' ORDER BY finished_at DESC LIMIT 5;"
```

2. If `last_error.code='SchemaInvalidExtractionError'`: the Vision Language Model is returning invalid JSON despite strict structured output. Check the model version pin; a model regression has happened. Pin to the previous known-good model.
3. If `last_error.code='VlmRateLimitedError'`: increase the retry budget (the queue worker's `max_attempts` setting) or add request shaping at the gateway.
4. Once root caused, requeue dead letters by updating their state:

```bash
docker compose exec postgres psql -U sidecar -d sidecar -c \
  "UPDATE agent_jobs SET state='queued', attempt_count=0, next_attempt_at=NOW() WHERE state='dead_letter' AND last_error->>'code' = '<the error code>';"
```

### 2.3 Orphan DocumentReference (FHIR row exists, no queue row)

**Symptom:** Grafana panel "FHIR docs without a job" reports a non-zero count, or a clinician reports a document is uploaded but never extracts.

**Diagnosis:** the upload handler's FHIR DocumentReference create succeeded but the subsequent `agent_jobs` insert failed before commit. The handler raises `UploadQueueError` (HTTP 503) so the client knows the job did not start, but the FHIR resource was already persisted by OpenEMR and cannot be rolled back from the sidecar.

**Fix:**

```bash
# 1. List orphans (DocumentReference rows with no corresponding agent_jobs row).
docker compose exec mariadb mysql -uroot -proot openemr -e "
  SELECT id, foreign_id AS patient_id, hash, created_at
  FROM documents
  WHERE id NOT IN (
    SELECT document_id FROM (
      SELECT DISTINCT document_id FROM agent_jobs
    ) j
  )
  ORDER BY created_at DESC LIMIT 20;
"
```

Then either:

- **Backfill** by inserting a queued `agent_jobs` row pointing at the orphan document_id (preferred — clinical content is preserved).
- **Delete** the orphan DocumentReference if the upload handler returned 503 to the client and the client retried (the client has a fresh document and the orphan is stale).

The handler logs `upload_queue_insert_failed` with the document_id at WARN level whenever this happens, so a real-time alert can fire without waiting for the periodic Grafana scan.

---

## Phase 3 — VLM extraction

### 3.1 Vision Language Model returns malformed JSON

**Symptom:** `extraction.parse_failures` span attribute is non-zero. Eval rubric `schema_valid` regresses.

**Diagnosis:** the VLM is occasionally returning text that does not match the strict structured output schema. Usually a model regression or a prompt drift.

**Fix:** the extractor's retry logic asks the model to repair its own output once with the validation error embedded; a second failure routes the job to `dead_letter`. Pin to the previous known-good model version while you investigate.

### 3.2 Extraction confidence universally low

**Symptom:** `extraction.confidence_p50` < 0.6 across multiple documents. Eval `factually_consistent` regresses.

**Diagnosis:** rendering DPI dropped, or the calibration set has shifted. Or the prompt was changed without updating the calibration.

**Fix:** confirm DPI in `sidecar/ingest/render.py` is still 300 for non-born-digital pages. If it is, run the calibration sweep (`scripts/calibrate_confidence_floor.py`) and adjust the floor in the schema.

---

## Phase 4 — RAG

### 4.1 Recall regression below floor

**Symptom:** `evals/golden_w2/recall_test.py` reports recall@5 < 0.92. CI fails.

**Diagnosis:** corpus update added a chunk that shadows a known answer, or the embedder version changed.

**Fix:** inspect the failing queries the script names; either re-tune the chunker for the offending source, or add the conflicting chunk to a quarantine list with a justifying issue.

### 4.2 Cohere rerank unavailable

**Symptom:** `retrieval.degraded=true` on every span. Grafana panel "Reranker availability" drops.

**Diagnosis:** Cohere outage, rate limit, or expired API key.

**Fix:** the retriever falls back to Reciprocal Rank Fusion order automatically; users see a weaker-ranked answer. Check the Cohere status page; rotate the API key if it expired. The eval suite has a `reranker_unavailable` probe; if that probe passes the fallback is healthy.

---

## Phase 7 — Sanitization and cost ceiling

### 7.1 Cost envelope hit

**Symptom:** `cost.envelope_hard_cutoff_active=true`. Users see HTTP 503 from `/chat`. PagerDuty alert.

**Diagnosis:** daily cost budget exhausted.

**Fix:**
1. Triage from the Grafana cost dashboard.
2. If the spike is legitimate (real clinical use): raise the envelope via the `COST_DAILY_USD_CAP` environment variable.
3. If the spike is a runaway loop: pause the agent (`docker compose stop sidecar-worker`), trace back to the loop source, fix, then resume.

### 7.2 Presidio unavailable

**Symptom:** the gateway returns HTTP 500 with code `PhiScrubFailoverError`. Logs show `presidio_analyzer.AnalyzerEngine` raising.

**Diagnosis:** the Presidio model bundle is missing or the recognizer registry failed to load.

**Fix (fail-closed is correct):**

```bash
# Reinstall the spaCy model.
poetry run python -m spacy download en_core_web_lg
# Restart the sidecar.
docker compose restart sidecar
```

---

## Phase 12 — Deployment

### 12.1 Hetzner cron deploy did not pull

**Symptom:** code on Hetzner is not the latest `master`. The deployed health check reports an old version.

**Fix:**

```bash
ssh root@5.161.253.237 "tail -200 /var/log/openemr-deploy.log"
# If the cron has not run, manually trigger it:
ssh root@5.161.253.237 "cd /opt/openemr && /opt/openemr/scripts/deploy.sh"
```

### 12.2 Disaster recovery from a fresh Hetzner instance

**Procedure:**

```bash
ssh root@<fresh-instance>
git clone https://github.com/scott-lydon/openemr /opt/openemr
cd /opt/openemr/clinical-copilot
bash scripts/bootstrap.sh
# Bootstrap restores: secrets from the encrypted vault, the Postgres dump
# from the most recent hourly backup, the guideline corpus from the
# baked-in seed, and brings up the Docker Compose stack.
```

Target: under 60 minutes from a fresh box to a healthy `/healthz` response.

---

## How to add a new entry

When you resolve an incident, append a new section to this file with:
- The symptom (what the alert or user said).
- The diagnostic command (or trace attribute) that proves it.
- The fix command (literal, not paraphrased).

Then commit with `Conventional Commits` style: `docs(runbook): add entry for <symptom>`.
