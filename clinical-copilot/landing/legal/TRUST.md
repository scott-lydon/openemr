# Trust — Clinical Co-Pilot

This page summarizes the security posture of the Clinical Co-Pilot service. It exists so a Customer's IT/security team can perform a vendor review without a back-and-forth email thread.

## 1. Architecture boundary

```
┌─────────────────────────┐         ┌────────────────────────┐
│   OpenEMR (Customer)    │         │   Clinical Co-Pilot     │
│                         │         │   Sidecar (us)          │
│  ┌──────────────────┐   │  FHIR   │  ┌─────────────────┐   │
│  │ Patient chart    │◄──┼─────────┼──┤ Snapshot fan-out│   │
│  │ (PHI)            │   │ TLS 1.2 │  │ Pairwise engine │   │
│  └──────────────────┘   │ SMART   │  │ Audit log       │   │
│  ┌──────────────────┐   │ Backend │  │ License check   │   │
│  │ oe-module-       │   │ Svcs    │  └─────────────────┘   │
│  │ clinical-copilot │   │         │                        │
│  └──────────────────┘   │         │  ┌─────────────────┐   │
│                         │         │  │ Postgres        │   │
└─────────────────────────┘         │  │ (audit + license)│   │
                                    │  └─────────────────┘   │
                                    └────────────────────────┘
                                                │
                                                │  (de-identified passages only)
                                                ▼
                                    ┌────────────────────────┐
                                    │ OpenAI Enterprise +    │
                                    │ ZDR (BAA-covered)      │
                                    │ Cohere Rerank (no PHI) │
                                    └────────────────────────┘
```

The sidecar pulls from OpenEMR over FHIR. The boundary is read-only. The sidecar never writes back to OpenEMR's PHI store.

## 2. Business Associate Agreement chain

| Party | Role | BAA / Equivalent |
|---|---|---|
| Customer | Covered Entity or Business Associate | Standard BAA (template in `BAA_TEMPLATE.md`) |
| Us | Business Associate | — |
| OpenAI, L.L.C. | Subprocessor (LLM inference) | OpenAI Enterprise BAA + Zero-Data-Retention |
| Stripe, Inc. | Subprocessor (billing) | No PHI sent |
| AWS or Hetzner | Subprocessor (hosting) | AWS BAA / Hetzner Data Processing Agreement |
| Cohere | Subprocessor (rerank) | No PHI sent — passages de-identified pre-call |

The de-identification step before the rerank call is critical: it lets us use a state-of-the-art reranker without expanding the BAA chain to a vendor that doesn't sign one for small SaaS vendors.

## 3. Security controls

### 3.1 Authentication

- **Customer → Sidecar:** SMART Backend Services `private_key_jwt` assertion (RFC 7523). No shared secrets. Public half of the keypair is registered in OpenEMR's `oauth_clients.jwks` column.
- **Clinician → Sidecar (browser):** short-lived (5 minute) HS256 task token minted by the OpenEMR module, bound to (user, patient, purpose).
- **Sidecar → OpenAI / Cohere:** API key from environment, encrypted on disk.

### 3.2 Authorization

- The OpenEMR module ACL-checks the clinician against the patient (`patients/demo`) on every launch.
- The sidecar verifies the task token signature, checks `purpose_of_use` membership against an allow list, and enforces a per-call rate limit.
- The license check (`/chat` `Depends(license_check)`) rejects calls when the license is missing, past_due, canceled, or revoked.

### 3.3 Encryption

- **In transit:** TLS 1.2+ between every component pair. The sidecar enforces certificate verification by default (`fhir_verify_ssl=true`).
- **At rest:** Postgres volumes (`copilot-pgdata`) are encrypted by the host's volume layer (LUKS on bare metal, AWS EBS encryption on AWS, default-on on Hetzner).
- **Module-side LLM API keys:** stored in OpenEMR's `module_oe_clinical_copilot_settings` table, encrypted via OpenEMR's `CryptoGen`.

### 3.4 Audit log

- Append-only Postgres table.
- Each row is hash-linked to the previous (SHA-256 over canonical JSON).
- 7-year retention.
- Chain head exported daily to AWS S3 Object Lock (governance mode, 7 year retention).

### 3.5 Vulnerability management

- Dependency scanning on every PR via GitHub Dependabot.
- `pip-audit` and `npm audit` runs in CI weekly.
- Container images built reproducibly with SBOM and signed provenance.
- Annual third-party penetration test (planned).

### 3.6 Logging and observability

- Structured JSON logs to stdout.
- OpenTelemetry traces (sampling rate configurable).
- Prometheus metrics exposed on `/metrics`.
- All logs scrubbed of PHI by the `phi_scrub` middleware before egress.

## 4. Compliance roadmap

| Item | Status | ETA |
|---|---|---|
| HIPAA-aligned controls | In place | — |
| OpenAI Enterprise BAA + ZDR | In place | — |
| Customer-facing BAA | Drafted; awaiting counsel redline | 2026-Q3 |
| Cyber liability insurance ($2M) | Open quote | 2026-Q3 |
| SOC 2 Type II audit | Not started | 2026-Q4 (12 months) |
| HITRUST CSF | Conditional on first hospital contract | 2027 |
| Annual pen test | Quote requested | 2026-Q3 |

## 5. Incident response

- Breach Notification Rule (HIPAA): notify Customer within 5 business days of discovery.
- Severity-1 incident: PagerDuty page → owner pickup within 15 minutes business hours, 30 minutes off-hours.
- Postmortem published within 14 days for every Sev-1.

## 6. Right to audit

Customer (or its designated auditor) may, on 30 days' written notice and at Customer's expense, audit our systems and procedures as they relate to Customer's PHI, once per calendar year and not more than once per audit cycle.

## 7. Contact

Security questions, vulnerability reports, or audit requests: relays.inanity.0n@icloud.com (PGP key on `/security` page).
