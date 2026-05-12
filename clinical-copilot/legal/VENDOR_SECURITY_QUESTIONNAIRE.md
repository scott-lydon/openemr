# Vendor Security Questionnaire — Pre-filled Responses

This document mirrors the Cloud Security Alliance's Consensus Assessments Initiative Questionnaire (CAIQ) v4.0 and the Google Vendor Security Assessment Questionnaire (VSAQ) so a hospital procurement reviewer can copy-paste the relevant section into their tracker. Keep this file in sync with `TRUST.md` and the BAA.

Format: `Q.<number> | <question> | <yes/no/partial> | <evidence/citation>`.

## Identity & Access Management

| # | Question | Answer | Evidence |
|---|---|---|---|
| IAM.1 | Multi-factor authentication enforced for all administrative access to production systems? | yes | Hetzner panel + AWS console both have MFA required; `gh` org has MFA-enforced for all members. |
| IAM.2 | Privileged access reviewed at least quarterly? | yes | Audit calendar entry; reviewer is the owner; reviewed list checked into `ops/access-reviews/`. |
| IAM.3 | Service accounts use short-lived credentials (≤ 1 hour) or signed JWTs? | yes | SMART Backend Services `private_key_jwt`; OpenAI uses key rotation; Stripe uses signed webhook secrets. |
| IAM.4 | Customer access uses SSO / SAML / OIDC? | partial | Native module login uses OpenEMR's existing auth. SAML/OIDC pass-through is on the Enterprise tier roadmap (Q4 2026). |
| IAM.5 | Account lockout after failed attempts? | yes | OpenEMR's default lockout policy applies to module access; sidecar token verification fails closed. |

## Encryption

| # | Question | Answer | Evidence |
|---|---|---|---|
| ENC.1 | Data encrypted in transit using TLS 1.2 or higher? | yes | `fhir_verify_ssl=true` default; CDN enforces TLS 1.2+ on the marketing site. |
| ENC.2 | Data encrypted at rest using AES-256 or stronger? | yes | Hetzner LUKS-encrypted volumes; AWS EBS default encryption; Postgres column encryption via `CryptoGen` on PHI-adjacent fields. |
| ENC.3 | Encryption keys managed in a Key Management System (KMS) with hardware-backed protection? | partial | KMS hosting on a future HSM-backed AWS KMS tier. Today, keys are stored under root-encrypted home directories with documented rotation procedure. |
| ENC.4 | Symmetric keys rotated at least annually? | yes | Documented annual rotation in `OPERATOR_GUIDE.md` §3. |

## Application Security

| # | Question | Answer | Evidence |
|---|---|---|---|
| APP.1 | Software development lifecycle includes code review? | yes | Every PR requires approval; CodeRabbit + human review enforced. |
| APP.2 | Static Application Security Testing (SAST) in CI? | yes | `bandit` for Python; PHPStan level 10 for PHP. |
| APP.3 | Dependency vulnerability scanning in CI? | yes | Dependabot for PHP/JS, `pip-audit` for Python, weekly. |
| APP.4 | Container images scanned before publication? | yes | `docker buildx` with `provenance: true` and SBOM; ghcr.io advanced security scanning enabled. |
| APP.5 | OWASP Top 10 risks reviewed? | yes | Annual review documented in `clinical-copilot/SECURITY.md`. |
| APP.6 | Penetration tested annually by an independent third party? | partial | First-year pen test scheduled Q3 2026 ($10K-$20K budget). |
| APP.7 | Bug bounty or coordinated vulnerability disclosure program? | yes | Email-only intake at `relays.inanity.0n@icloud.com` with 5-business-day acknowledgement SLA. |

## Logging & Monitoring

| # | Question | Answer | Evidence |
|---|---|---|---|
| LOG.1 | Application logs retained for at least 1 year? | yes | 7 years for audit log; 1 year for operational logs. |
| LOG.2 | Logs shipped to a tamper-evident store? | yes | Hash-chained audit log; daily anchor to AWS S3 Object Lock. |
| LOG.3 | Log access monitored for unusual patterns? | partial | Manual review today; SIEM tier on the SOC 2 roadmap. |
| LOG.4 | Sensitive data (PHI, secrets) redacted from operational logs? | yes | `phi_scrub` middleware redacts before egress; verified by `tests/test_phi_scrub.py`. |

## Business Continuity

| # | Question | Answer | Evidence |
|---|---|---|---|
| BCP.1 | Documented disaster recovery plan? | yes | `OPERATOR_GUIDE.md` §8. |
| BCP.2 | RPO ≤ 24 hours, RTO ≤ 4 hours? | yes | Daily backups; restore drill tested twice-yearly. |
| BCP.3 | Backups encrypted? | yes | AES-256 via storage provider default. |
| BCP.4 | Backup integrity tested? | yes | Twice-yearly restore drill documented. |

## Vendor Risk Management

| # | Question | Answer | Evidence |
|---|---|---|---|
| VRM.1 | Subprocessors listed publicly? | yes | `PRIVACY_POLICY.md` §3a. |
| VRM.2 | Subprocessors bound by written agreement? | yes | OpenAI Enterprise BAA, AWS BAA, Hetzner DPA, Stripe DPA. |
| VRM.3 | Subprocessor changes communicated 30 days in advance? | yes | Email to customer contacts on file. |

## HIPAA-Specific

| # | Question | Answer | Evidence |
|---|---|---|---|
| HIPAA.1 | Will execute a Business Associate Agreement (BAA)? | yes | Template at `legal/BAA_TEMPLATE.md`. |
| HIPAA.2 | HIPAA Security Rule controls in place? | yes | Documented in `legal/TRUST.md` §3. |
| HIPAA.3 | Breach Notification Rule timeline (5 business days)? | yes | BAA section 2(c). |
| HIPAA.4 | Right to audit? | yes | `legal/TRUST.md` §6. |
| HIPAA.5 | PHI handled only within US-based infrastructure? | partial | US default; EU customers can request EU-region hosting (Hetzner Germany). |
| HIPAA.6 | Sub-BAA signed with cloud provider? | yes | AWS BAA + Hetzner DPA. |
| HIPAA.7 | Sub-BAA signed with LLM provider? | yes | OpenAI Enterprise BAA + Zero-Data-Retention. |

## Compliance & Certifications

| # | Question | Answer | Evidence |
|---|---|---|---|
| CMP.1 | SOC 2 Type II audit? | partial | Audit scheduled Q4 2026; trust report available on request after completion. |
| CMP.2 | HITRUST CSF certification? | partial | Conditional on first hospital contract; quoted at ~$60K-$100K. |
| CMP.3 | ISO 27001? | no | Not pursued; resources prioritized on SOC 2 first. |
| CMP.4 | GDPR Article 28 processor agreement? | yes | Same template adapted for EU customers. |
| CMP.5 | State-specific privacy law compliance (CCPA, CDPA, CPA, CTDPA, UCPA)? | yes | Privacy Policy honors deletion/access requests within 30 days. |

## Physical Security

| # | Question | Answer | Evidence |
|---|---|---|---|
| PHY.1 | Production hosted in SOC 2 / ISO 27001 data centers? | yes | AWS (SOC 2 / ISO 27001 / HITRUST), Hetzner (ISO 27001). |
| PHY.2 | Visitor logging at data center? | yes | Operated by provider; we never have physical access. |
| PHY.3 | No production data on employee endpoints? | yes | Engineers connect via SSH/SSO; no production data syncs to laptops. |

## Personnel

| # | Question | Answer | Evidence |
|---|---|---|---|
| PER.1 | Background checks on all personnel with production access? | partial | Owner only today; will scale with hiring. |
| PER.2 | Annual security awareness training? | yes | Documented self-study covering HIPAA, OWASP Top 10, social engineering. |
| PER.3 | Confidentiality agreement signed by all personnel? | yes | Standard NDA on file. |
| PER.4 | Termination procedure revokes access within 24 hours? | yes | Documented in `ops/offboarding.md`. |

---

## Notes for the reviewer

- Items marked **partial** are on the public roadmap with an ETA.
- Items marked **no** are deliberate trade-offs; happy to discuss.
- Anything not on this list, email `relays.inanity.0n@icloud.com`.
