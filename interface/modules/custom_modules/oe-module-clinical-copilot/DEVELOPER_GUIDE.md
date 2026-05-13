# Developer Guide — Clinical Co-Pilot

Audience: someone extending the module or the sidecar.

## 1. Layout

```
oe-module-clinical-copilot/        ← this module (PHP, GPL-3.0)
├── composer.json                  ← PSR-4 to OpenEMR\Modules\ClinicalCoPilot
├── openemr.bootstrap.php          ← registers listeners + route + console command
├── README.md INSTALL.md OPERATOR_GUIDE.md CLINICIAN_GUIDE.md DEVELOPER_GUIDE.md
├── public/
│   ├── launch.php                 ← patient-summary button → sidecar
│   ├── refresh-token.php          ← chat UI token refresh
│   ├── toggle-modern-ui.php       ← optional modern Next.js dashboard switch
│   ├── admin.php                  ← admin settings page
│   └── store-document.php         ← HTTP doc-ingest endpoint
├── bin/
│   └── store-document.php         ← CLI doc-ingest entry point
└── src/
    ├── BootstrapService.php       ← wiring graph
    ├── ModuleSettings.php         ← private settings repo (replaces globals.inc.php)
    ├── TaskTokenMinter.php        ← HS256 JWT minter (wire-compatible with sidecar)
    ├── Listener/
    │   ├── PatientSummaryRenderListener.php
    │   ├── InstallListener.php
    │   └── UninstallListener.php
    ├── Console/
    │   └── ProvisionClinicalCoPilotApiClientCommand.php
    ├── Rest/
    │   ├── SnapshotController.php
    │   └── SnapshotRouteRegistrar.php
    └── Internal/
        └── _store_document_impl.php

clinical-copilot/                  ← sidecar (Python, private upstream)
├── sidecar/
│   ├── main.py                    ← FastAPI app + lifespan
│   ├── api/                       ← chat, chat_w2, documents, citations, billing
│   ├── agent/                     ← pair generator, judge, aggregator, LangGraph wiring
│   ├── verifier/                  ← source attribution + curated rule store
│   ├── audit/                     ← hash-chained audit log
│   ├── licensing/                 ← license check + state resolution
│   ├── migrations/                ← Alembic
│   └── observability/             ← OTel + Prometheus + PHI scrub
├── bff/                           ← Backend-for-Frontend
├── ui/                            ← chat.html, chat_w2.html
├── deploy/
│   └── docker-compose.openemr-sidecar.yml
├── landing/
│   └── index.html                 ← marketing site
├── legal/
│   ├── BAA_TEMPLATE.md
│   ├── TERMS_OF_SERVICE.md
│   ├── PRIVACY_POLICY.md
│   └── TRUST.md
└── evals/                         ← 3-layer eval suite + CI gate
```

## 2. PHP coding standards

The module follows OpenEMR's modern code standards (see `openemr/CLAUDE.md`):

- `declare(strict_types=1)` at the top of every file.
- PSR-4 autoload (`OpenEMR\Modules\ClinicalCoPilot\`).
- Domain primitives for PHI-adjacent identifiers (`PatientId`, etc.) — add as needed.
- No `$GLOBALS` access. Settings live in `ModuleSettings`.
- PHPStan level 10 across the module.
- Every public method has a return type annotation.

Run quality checks from the OpenEMR repo root:

```
composer phpstan
composer phpcs
composer rector-check
```

## 3. Sidecar Python coding standards

- `from __future__ import annotations` at the top of every module.
- mypy strict.
- ruff with the default Anthropic ruleset (E, F, W, I, B, UP, PL, RUF).
- Every public function has a typed signature and a docstring explaining the failure mode, not just the happy path.
- Errors raise specific exceptions (never bare `except: pass`).

Run from the sidecar root:

```
cd clinical-copilot
ruff check sidecar/
mypy sidecar/
pytest evals/
pytest tests/
```

## 4. Adding a new diagnostic prompt

AI prompts live in `clinical-copilot/sidecar/agent/prompts/`. Each file is a single Python module exporting a `Prompt` value object. The aggregator picks prompts based on the request's `purpose_of_use`.

To add a new use case:

1. Add a new purpose code to `ModuleSettings::KEY_PURPOSE_ALLOWLIST` default value.
2. Add a new prompt module under `sidecar/agent/prompts/<purpose>.py`.
3. Add a golden case under `clinical-copilot/evals/golden/` covering the new prompt.
4. Update the launch listener if you want a new button (`PatientSummaryRenderListener` currently only renders one button; a more granular dispatch would extend it).

The eval gate in `.github/workflows/w2-eval-gate.yml` runs the golden suite and refuses to merge if any golden case regresses.

## 5. Adding a new curated rule

The verifier owns a small curated rule store (`clinical-copilot/sidecar/verifier/rules/`). Each rule is YAML:

```yaml
id: rule-NSAID-CKD-warning
applies_when:
  patient_has_condition: ["N18*"]  # ICD-10 chronic kidney disease
  patient_has_medication: ["NSAID"]
fires:
  - severity: warning
    citation: "uptodate://nsaid-ckd-2024"
    message: "NSAID active in patient with CKD stage 3+. Reassess risk/benefit."
```

The verifier consumes every YAML at boot; there is no separate registration step. PR a new rule with a golden test case in `evals/golden/rules/`.

## 6. Modifying the audit log shape

The audit log is hash-chained. Changing the payload shape changes the hash. To stay backward-compatible:

- Never remove a field. Mark it deprecated and leave it null on new rows.
- Add new fields only via Alembic migration plus a `chain_version` bump.
- The `verify` command knows how to handle a chain that contains rows with multiple `chain_version`s; do not break that.

## 7. Releases

Tag `vN.M.O` on the master branch.

- `release-sidecar.yml` builds and pushes the sidecar + BFF images to ghcr.io.
- `release-module.yml` packages the module folder as a zip and attaches it to the GitHub Release.

Smoke-test the release before promoting `:latest`:

```
docker pull ghcr.io/scott-lydon/clinical-copilot-sidecar:vN.M.O
docker run --rm -p 8801:8801 -e COPILOT_LLM_PROVIDER=mock ghcr.io/scott-lydon/clinical-copilot-sidecar:vN.M.O
curl http://localhost:8801/diagnostic
```

Expect `version.git_hash` to match the tag's commit, `checks.auth_method == "private_key_jwt"`, and `checks.purpose_check_class == "membership_in_authorized_purposes"`.

## 8. Local dev quick-start

```
# 1. Bring up OpenEMR (existing flow)
cd docker/development-easy && docker compose up -d

# 2. Bring up the sidecar
cd ../../clinical-copilot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,postgres,observability]"
docker compose up -d copilot-postgres
alembic upgrade head
uvicorn sidecar.main:app --host 0.0.0.0 --port 8801 --reload

# 3. Register the OAuth client (one-time per OpenEMR install)
clinical-copilot/scripts/setup-openemr-client.sh

# 4. Install the OpenEMR module
# Admin → Modules → Register, Install, Enable.
# Open the gear icon, paste http://localhost:8801 as the Sidecar URL.
# Click Generate New Key on the JWT field, paste into .env COPILOT_BFF_JWT_SIGNING_KEY.
# docker compose restart copilot-sidecar
# Test Connectivity → expect HTTP 200.

# 5. Open any patient, click Clinical Co-Pilot (AI), ask a question.
```

## 9. Testing

| Layer | Where | How |
|---|---|---|
| Module PHP unit | `openemr/tests/Tests/Isolated/ClinicalCoPilot/` | `composer phpunit-isolated` |
| Sidecar Python unit | `clinical-copilot/tests/` | `pytest tests/` |
| Sidecar evals (golden cases) | `clinical-copilot/evals/golden/` | `pytest evals/` |
| Sidecar evals (mutation) | `clinical-copilot/evals/` | `mutmut run` (nightly via `w2-mutmut-nightly.yml`) |
| Integration | `clinical-copilot/tests/integration/` | `pytest tests/integration -k integration` |
| Security | repo-wide | `bandit -r clinical-copilot/sidecar`, `pip-audit`, `npm audit` |
