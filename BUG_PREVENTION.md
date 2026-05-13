# Bug / issue prevention — OpenEMR + Clinical Co-Pilot

Running checklist of past incidents and the rules they spawned. Every new
feature should be reviewed against this list before merge so the same bug
class does not re-appear.

## D — Deployment & module install

### D1. Clinical Co-Pilot module silently disabled on prod

**Issue (2026-05-13).** Clinical Co-Pilot launch.php returned HTTP 401
with `OpenEMR.WARNING: Access to module path for disabled module is
denied` (ModulesApplication.php:102). The module files were on disk in
`interface/modules/custom_modules/oe-module-clinical-copilot/` but the
`modules` table had no row for `oe-module-clinical-copilot`, so
`bootstrapCustomModules` rejected direct script access. The patient
summary listener also did not fire, so the launch button never rendered.

**Prevention.** A deploy that ships module files must also run
`Admin → Modules → Manage Modules → Register → Install → Enable` (or
the equivalent SQL: insert into `modules` with `mod_active=1`, then
trigger the install hook so `ModuleSettings::ensureSchema()` creates
`module_oe_clinical_copilot_settings`). The deploy script should fail
loudly when the module exists on disk but is missing from the table.

## A — Auth / OAuth

### A1. JWT audience mismatch when OpenEMR is fronted by a hostname proxy

**Issue (2026-05-13).** The sidecar's client-credentials JWT carried
`aud: "https://5.161.253.237/oauth2/default/token"` (raw IP, the
`COPILOT_OPENEMR_OAUTH_BASE` value), but OpenEMR's site URL was
`https://openemr.5-161-253-237.sslip.io`, so league/oauth2-server's
audience constraint rejected every token with "The token is not allowed
to be used by this audience". The Co-Pilot header showed
`HTTP 502: invalid_client`; every snapshot fetch failed.

**Prevention.** Whenever you move OpenEMR behind a new public hostname
(Caddy, nginx, sslip.io, real domain), also update every client's
`*_OAUTH_BASE` / `*_FHIR_BASE` env that signs the audience claim, and
restart those clients with `docker compose up -d --force-recreate`
(plain `restart` does NOT re-read `.env`). Pin this in a deploy-checklist
attached to the Caddyfile.

### A2. JWT signing key drift between OpenEMR module and sidecar

**Issue (2026-05-13).** Rotating the Co-Pilot module's JWT key via the
admin page "Generate New Key" button changes only the OpenEMR side;
without mirroring the new value into the sidecar's
`COPILOT_BFF_JWT_SIGNING_KEY` env and restarting, the sidecar verifies
the BFF→sidecar token with the old key and 401s every task.

**Prevention.** Treat the admin page's "Generate New Key" success
banner as a TODO: paste the new key into `clinical-copilot/.env`,
`docker compose up -d --force-recreate sidecar bff`, then confirm
`docker exec copilot-sidecar sh -c 'echo \$COPILOT_BFF_JWT_SIGNING_KEY'`
matches the module's stored value before closing the ticket.

## L — License gating

### L1. Self-hosted sidecar refused /chat with 402 because license row was missing

**Issue (2026-05-13).** Fresh sidecar install had no row in the
`licenses` table, so `resolve_license_state()` returned `missing` and
`license_check()` raised 402 on every /chat call. Diagnostic and
chart-error scans use the same route, so the entire AI surface 402'd
even though the model + DB + auth were all healthy.

**Prevention.** Self-hosted deploys should set
`COPILOT_LICENSE_BYPASS=true` in the sidecar `.env` as a documented
opt-out (per `licensing/__init__.py:91`). The install script for
self-hosters should default this on; managed deploys should default it
off and rely on Stripe webhooks to seed the row.

## D — Document push (chat PDF upload)

### D2. Sidecar pushed PDFs to the legacy store-document.php path

**Issue (2026-05-13).** After moving `store-document.php` from
`interface/clinical_copilot/` into the module's `public/` directory,
the sidecar's `COPILOT_OPENEMR_DOC_PUSH_URL` still pointed at the old
location and got HTTP 404 (Apache HTML page, not the endpoint). Chat
PDF uploads were "accepted by the sidecar but pushing the document into
OpenEMR failed" with `openemr_push_failed`.

**Prevention.** Whenever a module-internal endpoint changes location,
grep every env file (`/opt/openemr/clinical-copilot/.env`,
`docker-compose.openemr-sidecar.yml`, the module's `.env.example`) for
the old path before merging the move. Add a deploy-time smoke test that
POSTs an empty body to `\$COPILOT_OPENEMR_DOC_PUSH_URL` and asserts the
response is JSON (400/401/422 OK; 404 HTML = wrong path).
