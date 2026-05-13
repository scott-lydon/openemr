# Clinical Co-Pilot for OpenEMR

An AI diagnostic cross-check and chart-error scan for OpenEMR. Read-only against FHIR. Citations required for every answer. HIPAA-aligned audit trail.

## What it does

- **Diagnostic cross-check.** Ranks candidate diagnoses with citations into the chart instead of asking the LLM to produce a list. Surfaces the top three with citations to FHIR resources in the patient's chart.
- **Chart-error scan.** Flags inconsistencies (e.g. a medication active for a contraindicated condition) and proposes a one-paragraph clinician-facing explanation.
- **Follow-up questions.** A freeform chat surface scoped to the patient's chart.
- **Document ingest.** Drag-and-drop a PDF into the chat; the sidecar extracts structured fields and writes them back via FHIR transaction bundle (with the clinician's confirmation).

## Architecture

```
Patient summary ─[ launch button ]─▶ /launch.php
   ▼ (signed task token in URL fragment)
Sidecar (FastAPI + LangGraph + Postgres)
   ▼ (FHIR fan-out)
OpenEMR FHIR API ───────────────────────▶ AI engine ─[ LLM (OpenAI Enterprise BAA) ]
                                             │
                                             ▼ Audit log (hash-chained, 7-year retention)
```

The sidecar is a separate process. It can run in the same Docker network as OpenEMR, on a separate host, or on our hosted infrastructure.

## Install

See [INSTALL.md](INSTALL.md).

## Configure

See the module's admin page (gear icon next to Clinical Co-Pilot in `Admin → Modules → Manage Modules`).

## More

- [OPERATOR_GUIDE.md](OPERATOR_GUIDE.md) — running the sidecar, key rotation, backup, troubleshooting.
- [CLINICIAN_GUIDE.md](CLINICIAN_GUIDE.md) — how clinicians use the launch button, what the citations mean, when to override.
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) — extending the rule store and adding new diagnostic prompts.
- [`legal/BAA_TEMPLATE.md`](../../../../clinical-copilot/legal/BAA_TEMPLATE.md) — the BAA template Customer signs.
- [`legal/TRUST.md`](../../../../clinical-copilot/legal/TRUST.md) — security posture summary for IT vendor reviews.

## License

GPL-3.0-or-later for the PHP module. The sidecar is separately licensed; pricing at https://copilot.scott-lydon.dev.
