# Privacy Policy — Clinical Co-Pilot

**Effective date:** 2026-05-12. Last updated: 2026-05-12.

**IMPORTANT — DRAFT TEMPLATE.** Have these reviewed by counsel before publishing.

This Privacy Policy ("Policy") describes how Scott Lydon ("we", "us") collects, uses, and discloses information when you use the Clinical Co-Pilot service ("Service"). This Policy is focused on non-PHI information — Protected Health Information (PHI) handling is governed by the separate Business Associate Agreement (BAA).

## 1. Information we collect

### 1.1 Account information

When a clinic operator signs up, we collect the operator's name, email address, organization name, billing address, and Stripe customer identifier. We do not collect or store payment card numbers; Stripe handles payment.

### 1.2 Usage telemetry

We collect non-PHI operational telemetry from the sidecar:

- HTTP method and response code for each request
- Latency
- License key (hashed)
- Plan tier
- Anonymized organization id
- Container image version

We do NOT include patient identifiers, clinician identifiers, free-text content, FHIR resource bodies, or any other PHI in telemetry.

### 1.3 Audit log

The Service maintains a hash-chained audit log of every clinician question and AI answer. This log contains PHI; its retention, access, and disclosure are governed by the BAA, not this Policy.

### 1.4 Cookies and local storage

The marketing website (`copilot.scott-lydon.dev`) uses essential cookies for session management only. We do not use third-party analytics or advertising cookies.

## 2. How we use information

We use the information we collect to:

a. Operate, secure, and improve the Service.
b. Bill the Subscription Plan.
c. Communicate with clinic operators about updates, security advisories, and billing.
d. Comply with legal obligations.

We do **not** use customer data, telemetry, or PHI to train any AI model.

## 3. Sharing of information

We share information only with:

a. **Subprocessors who help us operate the Service.** Each subprocessor is bound by a written agreement that requires confidentiality and (where PHI is involved) execution of a downstream BAA. Current subprocessors:

   - **OpenAI, L.L.C.** — language model inference under Enterprise BAA + Zero-Data-Retention (ZDR). PHI permitted; not used for training; retained zero days.
   - **Stripe, Inc.** — payment processing. PHI prohibited.
   - **GitHub, Inc.** — release distribution and source hosting. PHI prohibited.
   - **Amazon Web Services, Inc.** — infrastructure hosting (if used for the hosted sidecar tier). PHI permitted under AWS BAA.
   - **Hetzner Online GmbH** — infrastructure hosting (if used for the hosted sidecar tier). PHI permitted under Hetzner Data Processing Agreement.

b. **Recipients required by law.** We may disclose information when required by a subpoena, court order, or other legal process. We will give Customer notice before disclosure where legally permitted.

c. **A successor entity** in connection with a merger, acquisition, or asset sale. The successor will be bound by this Policy and the BAA.

We do not sell or rent personal information to third parties.

## 4. Security

We maintain administrative, technical, and physical safeguards designed to protect personal information against unauthorized access, disclosure, alteration, and destruction. Highlights:

- Encryption in transit (TLS 1.2+) and at rest (AES-256).
- SMART (Substitutable Medical Apps and Reusable Technology) on FHIR Backend Services for OpenEMR token exchange (private_key_jwt assertion; no shared secrets).
- Hash-chained append-only audit log with offsite anchoring.
- Quarterly access reviews; annual penetration test.

Security is necessarily best-effort; no system is perfectly secure. We will notify Customer of any security incident affecting their data within five (5) business days of discovery (sooner where required by HIPAA Breach Notification Rule).

## 5. Retention

- **Account information:** retained for the duration of the Subscription Plan plus 12 months for billing purposes.
- **Telemetry:** retained for 90 days, then automatically purged.
- **Audit log:** retained for seven (7) years per HIPAA-aligned policy.

## 6. Your rights

Depending on your jurisdiction, you may have rights including:

a. Access to the personal information we hold about you.
b. Correction of inaccurate information.
c. Deletion (subject to legal and contractual retention obligations).
d. Data portability.
e. Objection to processing.

To exercise these rights, contact relays.inanity.0n@icloud.com. We will respond within 30 days.

## 7. International transfers

If you access the Service from outside the United States, your data may be transferred to and processed in the United States. Where required, we rely on Standard Contractual Clauses or equivalent mechanisms.

## 8. Children's privacy

The Service is not directed to individuals under the age of 18. We do not knowingly collect personal information from children outside of a clinical context (where pediatric PHI is governed by HIPAA and the BAA).

## 9. Changes to this Policy

We may update this Policy. We will provide at least thirty (30) days' notice for material changes via email to clinic operators on file.

## 10. Contact

Privacy questions: relays.inanity.0n@icloud.com.

Data Protection Officer (if you are subject to GDPR or equivalent): same contact.
