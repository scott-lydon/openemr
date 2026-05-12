# OpenEMR Patient Dashboard

A modern reimplementation of OpenEMR's patient dashboard. Built for the W2 Surprise Challenge of the Gauntlet AgentForge program.

**Stack:** Next.js 16 (App Router) · React 19 Server Components · Auth.js 5 (OAuth2 + OIDC) · Zod 4 · Tailwind 4 · Docker.

The graded defense for the framework choice lives in [`PATIENT_DASHBOARD_MIGRATION.md`](./PATIENT_DASHBOARD_MIGRATION.md).

---

## What it does

- Authenticates clinicians via OpenEMR's OAuth2 / OpenID Connect server (Authorization Code + PKCE).
- Renders a persistent patient identity bar with name, DOB, sex, MRN, and active status.
- Streams six clinical cards independently from OpenEMR's FHIR R4 surface:
  - Allergies (`AllergyIntolerance`)
  - Problem List (`Condition`)
  - Medications (`MedicationRequest`)
  - Prescriptions (e-prescribed subset of `MedicationRequest`)
  - Care Team (`CareTeam`)
  - Encounter History (`Encounter`) — the +1 of choice
- Adds zero database calls. Every shard goes through OpenEMR's existing public APIs.

## Quick start

```bash
# 0. OpenEMR up (separate stack — leaves the EMR untouched):
cd ../docker/development-easy && docker compose up --detach --wait
# OpenEMR will be at http://localhost:8300

# 1. Provision an OAuth client + write .env.local in one step:
cd ../patient-dashboard
bash scripts/register-oauth-client.sh
# Open the admin URL it prints, click "Enable Client" once.

# 2. Dev server (hot reload):
npm install
npm run dev
# → http://localhost:3000

# OR — build the production image and run via docker compose:
docker compose up --detach --build
# → http://localhost:8400
```

## Files of interest

| Path | What it is |
|---|---|
| `src/auth.ts` | Auth.js v5 OIDC config + token refresh against OpenEMR. |
| `src/lib/fhir/client.ts` | Typed FHIR client. Reads token from session, validates with Zod. |
| `src/lib/fhir/schemas.ts` | Zod schemas for every FHIR resource the dashboard consumes. |
| `src/lib/fhir/parsers.ts` | FHIR resource → domain primitive transforms. Encodes every quirk we know. |
| `src/lib/fhir/errors.ts` | Typed error classes. Catch sites use `instanceof`. |
| `src/lib/env.ts` | Validated environment configuration. Throws actionable errors on misconfiguration. |
| `src/middleware.ts` | Auth gate for every non-public route. |
| `src/app/login/page.tsx` | OIDC sign-in. |
| `src/app/page.tsx` | Patient picker landing page. |
| `src/app/patient/[id]/page.tsx` | Main dashboard. Patient header + six streamed cards. |
| `src/app/patient/[id]/error.tsx` | Page-level error boundary. |
| `src/app/patient/by-pid/[pid]/page.tsx` | Convenience: numeric pid → UUID resolver redirect. |
| `src/components/patient-header.tsx` | The persistent identity bar. |
| `src/components/cards/*.tsx` | One Server Component per card. |
| `Dockerfile` | Multi-stage build → ~120 MB standalone runtime. |
| `docker-compose.yml` | Local dev wiring. Reaches OpenEMR via host gateway. |
| `scripts/register-oauth-client.sh` | Dynamic registration + .env.local bootstrap. |

## Health check

```
GET /healthz
```

Returns:

```json
{
  "ok": true,
  "service": "openemr-patient-dashboard",
  "build": "<git sha or 'dev'>",
  "time": "2026-05-06T12:34:56.789Z"
}
```

`/healthz` deliberately does not call OpenEMR. It reflects the dashboard's own readiness, which is what container orchestrators want.

## Testing locally against the demo patients

The Clinical Co-Pilot work seeded three patients in the local OpenEMR. Their pids:

- `87413` — Barbara Boston (gout case)
- `87414` — Suzie Sanchez (osteoporosis case)
- `87415` — Demo Patient (penicillin allergy)

The landing page links to each via the `/patient/by-pid/<pid>` resolver route, so the dashboard works against a fresh install where the random Patient.id UUID differs from a previous seed run.

## License

Same license as the parent OpenEMR fork (GNU GPL v3). See the repo root for the LICENSE file.
