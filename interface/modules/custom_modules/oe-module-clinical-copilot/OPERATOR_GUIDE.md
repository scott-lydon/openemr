# Operator Guide — Clinical Co-Pilot

Audience: the person responsible for running OpenEMR and the Clinical Co-Pilot sidecar at the clinic. You install once; you operate forever.

## 1. Topology choices

Three production topologies. Pick one before you install.

| Topology | Sidecar runs | OpenEMR runs | When to pick |
|---|---|---|---|
| **All-in-one** | Same Docker host as OpenEMR, same compose project | Same host | One clinic, < 200 charts/day. |
| **Co-located sidecar** | Same Docker host as OpenEMR, separate compose project | Same host | Two or more clinics on the same host. |
| **Hosted sidecar** | Our infrastructure (`https://api.copilot.scott-lydon.dev`) | Customer host | You have no Docker host you want to run our software on; you trust our BAA chain. |

The PHP module behaves identically across all three. The choice is operational, not architectural.

## 2. Environment variables

The sidecar reads its configuration from environment variables. The full list lives in `clinical-copilot/.env.example`. The non-obvious ones:

| Variable | Default | Meaning |
|---|---|---|
| `COPILOT_BFF_JWT_SIGNING_KEY` | (empty) | MUST exactly match the `JWT Signing Key` shown on the module's admin page. Rotate via `Generate New Key` on that page; mirror here. |
| `COPILOT_LLM_PROVIDER` | `mock` | `openai`, `azure-openai`, `anthropic`, or `mock`. |
| `OPENAI_API_KEY` | (empty) | Required if `COPILOT_LLM_PROVIDER=openai`. |
| `COPILOT_DATABASE_URL` | `postgresql+psycopg://copilot:copilot@copilot-postgres:5432/copilot` | The sidecar's Postgres URL. Alembic uses it too. |
| `COPILOT_LICENSE_KEY` | (empty) | The license key value matching what you pasted into the OpenEMR module's admin page. Without it the sidecar rejects `/chat` with HTTP 402. |
| `STRIPE_WEBHOOK_SECRET` | (empty) | Required if you operate the hosted tier and want Stripe webhooks to update license rows. Self-hosters can leave empty. |
| `COPILOT_LICENSE_BYPASS` | `false` | Self-host opt-out from license check. Set to `true` only if you self-host AND you have a written agreement allowing it. |
| `COPILOT_DISABLE_INGEST_WORKER` | `false` | Set `true` to disable the document ingest worker on a sidecar that does not need it. |

## 3. Key rotation

### 3.1 JWT signing key

```
OpenEMR module admin → "Generate New Key" → copy value
SSH to sidecar host → edit .env → COPILOT_BFF_JWT_SIGNING_KEY=<paste>
docker compose restart copilot-sidecar
OpenEMR module admin → "Test Connectivity" → expect HTTP 200 + private_key_jwt
```

During the rotation window any in-flight task tokens fail with HTTP 401. The chat UI detects this and prompts the clinician to click "Refresh" which round-trips through `refresh-token.php` and gets a new token. Plan rotations for low-traffic windows but it does not require downtime.

### 3.2 SMART Backend Services keypair

The sidecar's OAuth client uses a private key + JWKS pair to authenticate to OpenEMR's `/token` endpoint. To rotate:

```
clinical-copilot/scripts/setup-openemr-client.sh --rotate
```

This generates a fresh RSA 2048 keypair, re-runs the provisioning command (which rewrites `oauth_clients.jwks`), and restarts the sidecar. Exit code 3 means the new key was generated but the verification step failed — check `/diagnostic`.

### 3.3 LLM API key

```
OpenEMR module admin → LLM API Key → paste new value → Save
SSH to sidecar host → edit .env → OPENAI_API_KEY=<paste>
docker compose restart copilot-sidecar
```

The OpenEMR-side value is the source of truth; the sidecar-side mirror exists because the sidecar reads from environment, not from OpenEMR. The module's admin page encrypts the key at rest via OpenEMR's `CryptoGen`.

## 4. Backup

### 4.1 What to back up

| Thing | Where | How often | Why |
|---|---|---|---|
| OpenEMR DB | Your existing OpenEMR backup script | Daily | PHI |
| Sidecar Postgres (`copilot-pgdata`) | `pg_dump` to S3 / Backblaze | Daily | Audit log + license state |
| `.env` files | Encrypted secret store (1Password, Vault, etc.) | On change | Recovery |
| `.keys/` directory | Encrypted secret store | On rotation | SMART Backend Services keypair |

### 4.2 Restore drill

Run twice a year. The first time you restore, you discover what is missing.

```
# 1. On a fresh host, install Docker and clone the openemr repo.
# 2. Restore OpenEMR DB.
# 3. Restore sidecar Postgres.
# 4. Restore .env + .keys/.
# 5. Boot:
docker compose -f clinical-copilot/deploy/docker-compose.openemr-sidecar.yml up -d
# 6. Verify /diagnostic returns the expected git_hash and license_state=ok.
```

If the audit log's chain-head doesn't match the offsite anchor (S3 Object Lock), you have either a corrupt restore or a tampered chain. Stop and investigate.

## 5. Audit log

### 5.1 Schema

`audit_entries` is append-only. Each row has:

- `id` (UUID v4)
- `prev_hash` (sha256 of the previous row's canonical JSON)
- `row_hash` (sha256 of this row's canonical JSON, including `prev_hash`)
- `created_at` (UTC timestamp)
- `actor_user_id` (the clinician)
- `patient_id` (FHIR resource id)
- `purpose_of_use`
- `event_type` (`chat_in`, `chat_out`, `document_ingest`, `judge_failed`, etc.)
- `payload_json` (the canonicalized event payload)

Verify the chain:

```
docker compose exec copilot-sidecar python -m sidecar.audit.verify --since 7d
```

Exit code 0 = chain intact. Non-zero = a row's hash does not match its content.

### 5.2 Offsite anchor

A daily systemd timer (sample in `clinical-copilot/scripts/anchor-audit-head.sh`) exports the latest chain head to AWS S3 Object Lock (governance mode, 7 year retention). The S3 bucket should be in a separate AWS account from the sidecar host's account so a compromised sidecar host cannot delete the anchor.

## 6. Observability

- **Logs.** `docker logs copilot-sidecar -f` shows structured JSON.
- **Metrics.** `curl http://localhost:8801/metrics` exposes Prometheus metrics.
- **Traces.** OpenTelemetry traces are exported to whatever endpoint `OTEL_EXPORTER_OTLP_ENDPOINT` points at (defaults to disabled).
- **Status page.** Public `/status` (planned, via Better Stack).

Useful queries:

```
# 5-minute window of failed /chat calls
docker logs copilot-sidecar --since 5m 2>&1 | grep '"status":[45]' | jq .

# License-state distribution across all customers (hosted tier only)
docker compose exec copilot-postgres psql -U copilot -c "select status, count(*) from licenses group by 1;"
```

## 7. Troubleshooting

| Symptom | First place to look | Most-common cause |
|---|---|---|
| Launch button missing | Module admin page → Sidecar URL field | Sidecar URL is empty |
| Launch button 503 | Module admin page → JWT Signing Key | Empty / placeholder key |
| `/chat` returns 401 with `task_token_invalid` | `docker logs copilot-sidecar` | Signing key mismatch between OpenEMR and sidecar |
| `/chat` returns 402 | `/diagnostic` → `checks.license_state` | License missing / past_due |
| Document ingest queues but never extracts | Sidecar logs for `ingest_worker_*` | `COPILOT_DISABLE_INGEST_WORKER=true` set; OR LLM provider key wrong |
| `Test Connectivity` HTTP 0 / curl error | OpenEMR host network reachability | Docker network not bridged correctly |

## 8. Disaster

Worst-case scenario: the sidecar host is on fire.

1. Spin up a new host (any Docker-capable VM).
2. Restore `.env` and `.keys/` from your secret store.
3. Restore `copilot-pgdata` from the latest daily dump.
4. `docker compose up -d` against the same `docker-compose.openemr-sidecar.yml`.
5. Update the OpenEMR module's admin page → Sidecar URL → new host's URL.
6. `Test Connectivity` → expect HTTP 200.

Total recovery time, well-rehearsed: 30 minutes. Hourly RPO for the audit log if you backup that often.
