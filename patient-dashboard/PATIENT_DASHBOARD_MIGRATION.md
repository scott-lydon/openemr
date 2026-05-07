# PATIENT_DASHBOARD_MIGRATION.md

## Defense for the Patient Dashboard reimplementation

> **Audience:** Gauntlet reviewers grading the W2 Surprise Challenge.
> **Repository path:** `patient-dashboard/` at the OpenEMR fork root.
> **Source brief:** *AgentForge — Clinical Co-Pilot W2 — Surprise Challenge: Modernize the Patient Dashboard*.

---

## 0. One-paragraph summary

The patient dashboard ships as a Next.js 16 application using the App Router, React Server Components, Auth.js v5 against OpenEMR's OAuth2 / OpenID Connect server, Zod for runtime FHIR validation, Tailwind v4 for styling, and a multi-stage Docker image that drops a roughly 120 MB standalone runtime onto the same host as the OpenEMR stack. Every clinical card (Allergies, Problem List, Medications, Prescriptions, Care Team, plus the +1 Encounter History) is an async Server Component wrapped in its own Suspense boundary, so each FHIR shard fetches and streams to the browser independently. The patient header renders first because every card depends on a valid Patient resource; if Patient resolution fails the page surfaces a single error rather than six broken cards. The OpenEMR backend is unmodified. The presentation layer moved off PHP. The defense for that move follows.

---

## 1. What was inherited and what was replaced

The legacy patient dashboard at `interface/patient_file/summary/demographics.php` is a Smarty / Twig hybrid rendered server-side from PHP. It composes globals (`$_SESSION`, `$GLOBALS`), reads MySQL through ADODB and Doctrine DBAL surface APIs, and ships approximately 1,400 lines of imperative PHP plus inline jQuery for tab-switching and demographic edits. The brief said not to touch the backend; this rebuild replaces only the rendering layer.

What stayed: every endpoint the dashboard reads (`/oauth2/default/*` for auth, `/apis/default/fhir/*` for data) is OpenEMR's own. The reimplementation makes zero direct database calls and adds no internal endpoints. If OpenEMR's REST and FHIR surfaces change, this dashboard moves with them.

What changed: PHP / Smarty rendering became React Server Components with Tailwind. Session-based auth became OAuth2 + OIDC with PKCE. Hand-rolled error pages became Suspense + per-card error boundaries. Imperative jQuery for tab-switching became a CSS grid that needs no client JavaScript at all. The runtime is one Linux binary plus Node, deployable behind any reverse proxy.

---

## 2. The framework choice and why

### 2.1 What I picked

**Next.js 16 (App Router) + React 19 Server Components + Auth.js 5 + Zod 4 + Tailwind v4.**

### 2.2 First-principles reasons

The dashboard's hottest loop is "fetch a half-dozen FHIR shards, render six cards, stream HTML to a clinician on potentially mediocre clinic broadband." Server Components map onto that workload exactly. Each card is `async`, awaits its FHIR query, and its HTML is flushed to the response stream as soon as the shard returns. The browser sees the patient header first, then individual cards pop in as their shards resolve. That is the same UX shape Phoenix LiveView gives you, but without learning Elixir for a one-week deliverable.

The rendering model also handles the most common OpenEMR FHIR failure mode (a slow Conditions shard on a patient with hundreds of rows) without ceremony. Wrapping each card in `<Suspense>` means a slow Conditions shard delays only the Problem List card; the other five render. PHP's stock model, by contrast, tries to assemble the full DOM before flushing.

Type safety is the second axis. OpenEMR FHIR responses vary widely in shape across installs and versions. `Patient.name` may be empty, single-element with only `text`, or carry `family` without `given`. The reimplementation models each domain primitive (PatientHeader, Allergy, Problem, Medication, Prescription, CareTeamMember, Encounter) as a TypeScript type, validates the wire response with Zod at the network boundary, and parses into the primitive via a dedicated parser. The cards never see a raw FHIR shape. This is the same discipline the Co-Pilot sidecar enforces in Python with Pydantic; this build mirrors it in TypeScript with Zod.

### 2.3 Reviewer challenges, answered

**"Why not React (CSR) or Next.js Pages Router?"**
Both would force the dashboard into a fetch-then-hydrate model where the browser waits for JavaScript before rendering anything. Server Components stream HTML before any JavaScript loads. For a clinical user who needs the patient identity bar visible *immediately*, this is the better default.

**"Why not SvelteKit?"**
Honest answer: SvelteKit is a fine choice and the bundle would be smaller. Two reasons it lost: (1) the FHIR ecosystem is most mature in TypeScript libraries Next can consume directly (`@types/fhir`, fhir-kit-client patterns, etc.); (2) the existing OpenEMR fork already has a Co-Pilot sidecar with TypeScript-flavoured patterns the team is becoming familiar with, so consolidation on TS reduces the project's per-language surface.

**"Why not Vapor (Swift on the server)?"**
The author's stated language preference is Swift, and that was the original handoff recommendation. The framework was reconsidered for this project because (a) Vapor's FHIR Codable layer would need to be hand-rolled (no first-class FHIR R4 model library), (b) Swift on Linux does not have a peer to React Server Components (server-rendered Leaf templates plus HTMX comes close but is two libraries, not one), and (c) iteration speed in a one-week sprint mattered more than language preference. The framework decision is project-specific; future Cooperative-Co-Pilot work in Swift remains on the table.

**"Why not Phoenix LiveView?"**
LiveView is the strongest pure-server-rendered streaming framework on the market. It lost on time-to-first-feature: Elixir / OTP would be the team's first Erlang-flavoured runtime, and the dashboard does not need persistent WebSocket sessions for an MVP. If the dashboard later grows real-time chart-update features (a nurse adds a med, the doctor's dashboard refreshes), LiveView becomes the right rewrite target. For "render a snapshot of FHIR data," Server Components are sufficient.

**"PHP 8.4 has types now."**
True. PHP's type system is still runtime, not compile-time. A static analyser like PHPStan is a check; TypeScript is a type system. The two are not the same axis. More importantly, OpenEMR's legacy patterns (`$GLOBALS`, `$_SESSION` as service locator, untyped arrays passed through Smarty templates) are pervasive in the existing code and would have to be opted out of file-by-file. A clean Next.js project starts with strict types from line one.

**"Won't a Node runtime increase the operational surface?"**
The standalone Next.js bundle plus Node runtime is one container, one process tree, ~120 MB on disk. The OpenEMR PHP-FPM stack is meaningfully larger. The dashboard's container is independent of OpenEMR's, so a dashboard restart never disturbs the EMR. Operational surface increases by roughly the cost of a single sidecar.

### 2.4 What was gained by moving away from PHP

- **Compile-time guarantees.** TypeScript catches the kind of typos that the legacy dashboard's "Demographics displayed two patients' DOB after a copy-paste in 2019" debug stories grew out of.
- **Composition.** Six self-contained card Server Components compose into a grid; reordering them is a CSS change, not a Smarty template surgery.
- **Streaming.** A slow shard does not block the render. The legacy dashboard's "blank page until the slowest query returns" failure mode disappears.
- **One source of styling.** Tailwind v4 plus design tokens replaces the legacy mix of inline styles, `style.css` overrides, and Bootstrap fragments.
- **Auth as a library, not a global.** `auth()` returns an `ExtendedSession` typed object. The legacy `AuthUtils::isAuthenticated()` returned a boolean; downstream code re-derived user identity from `$_SESSION` everywhere.
- **Modern accessibility primitives.** Server-rendered `aria-label`, semantic landmarks, and skeleton states with `sr-only` text. The legacy dashboard used `<table>` for layout in places.
- **Container-native deploy.** One Dockerfile, one image, one health check. The legacy app ships as part of a much larger PHP-FPM image.

### 2.5 What it cost

- **Two-runtime operational surface.** PHP-FPM and Node now both run on the same host. The dashboard stays decoupled (its restart never disturbs the EMR), but log aggregation and monitoring need to cover two stacks.
- **Bundle size.** Next.js plus React plus Tailwind plus Auth.js plus Zod is a richer dependency tree than a PHP install. The standalone build mitigates this (the production image carries only what the runtime needs), but the dependency graph is real.
- **Token lifecycle.** OAuth2 token expiry and refresh introduce a class of bug ("session expired mid-fetch") that the cookie-session legacy dashboard did not have. Auth.js handles refresh correctly, but a misconfigured `offline_access` scope means clinicians get bounced to login every hour.
- **TypeScript's structural typing.** TS does not stop two different fields with the same shape from being confused at the type level. Domain primitives (PatientHeader vs Allergy) make this rare in practice; the parsers funnel every field through a typed shape before render. The discipline is opt-in, not enforced by the language.
- **OpenEMR-specific FHIR quirks live in this codebase.** The Condition mapper's HTTP 500 on `category=` filters, the difference between `Patient/{uuid}` and `Patient?_id={uuid}`, and the medication-name fallback chain are all known to this dashboard's parsers because the dashboard rediscovered them from the Co-Pilot sidecar's notes. A future OpenEMR mapper change will surface as a Zod validation error with a precise path; that is the diagnostic surface this build invests in.

---

## 3. Architecture overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Browser                                                                │
│  • First paint: HTML for header + skeleton cards                        │
│  • Streamed: each card's HTML as its FHIR shard returns                 │
└─────────────────────────────┬───────────────────────────────────────────┘
                              │ HTTPS
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Next.js 16 dashboard (Node)                                            │
│  /login                ─ Auth.js OIDC sign-in                           │
│  /                     ─ Patient picker                                 │
│  /patient/[uuid]       ─ Header + 6 streamed cards                      │
│  /patient/by-pid/[pid] ─ Resolver redirect                              │
│  /api/auth/*           ─ Auth.js handlers                               │
│  /healthz              ─ Liveness + build sha                           │
│                                                                         │
│  src/lib/fhir/         ─ Typed FHIR client + Zod schemas + parsers      │
│  src/auth.ts           ─ OIDC config + token refresh                    │
│  src/middleware.ts     ─ Authenticated route gate                       │
└──────┬──────────────────────────────────────┬───────────────────────────┘
       │ OAuth2 / OIDC                        │ FHIR R4
       ▼                                      ▼
┌──────────────────────┐               ┌──────────────────────────────────┐
│ OpenEMR              │               │ OpenEMR FHIR mapper              │
│ /oauth2/default/*    │               │ /apis/default/fhir/*             │
│  • /authorize        │               │  Patient, Condition,             │
│  • /token            │               │  AllergyIntolerance,             │
│  • /userinfo         │               │  MedicationRequest, CareTeam,    │
│  • /jwk              │               │  Encounter                       │
│  • /registration     │               │                                  │
└──────────────────────┘               └──────────────────────────────────┘
```

### 3.1 Authentication

OAuth2 Authorization Code with PKCE, confidential client, standard OIDC discovery. Auth.js v5 stores the access token in an encrypted JWT cookie session. Refresh is handled automatically when the access token is within 60 seconds of expiry. A failed refresh marks the session with `error: "RefreshAccessTokenError"` and `/login` shows the user a friendly message rather than a generic 500.

Scopes requested:

```
openid profile fhirUser offline_access
user/Patient.read user/Condition.read user/MedicationRequest.read
user/AllergyIntolerance.read user/Observation.read user/Encounter.read
user/CareTeam.read user/Practitioner.read
```

`user/*` rather than `patient/*` so the clinician can browse multiple patient charts in one session. This matches the legacy dashboard's permission model (a clinician sees any chart their ACL covers) and avoids forcing a fresh OAuth dance per patient.

### 3.2 FHIR client

`src/lib/fhir/client.ts` is server-only (`import "server-only"`). Reads the access token from `auth()`, fans out parallel requests per shard, validates each Bundle against a Zod schema, and parses entries into domain primitives. Every failure mode is its own error class:

- `FhirNoSession` — caller invoked the client without an authenticated session.
- `FhirNotFound` — 404 from the upstream.
- `FhirAuthFailure` — 401/403, typically a missing scope.
- `FhirUpstreamFailure` — any other 4xx/5xx.
- `FhirTransportFailure` — DNS, TLS, or connection failure.
- `FhirValidationFailure` — 200 OK but the body did not match the Zod schema; carries the failing path.

The error classes are catch-grouped via a base `FhirError`. Per-card error boundaries render a `<CardError>` rather than crashing the page.

### 3.3 Streaming

`src/app/patient/[id]/page.tsx` awaits the patient header (the page can't render meaningfully without it), then renders six `<Suspense>` boundaries each wrapping an async card. Each card is its own server component fetching its own FHIR shard. Next.js streams the HTML as each card resolves. A slow shard delays only its own card.

### 3.4 Parsers

The most subtle bugs in any FHIR consumer come from sparse-or-missing fields. The parsers at `src/lib/fhir/parsers.ts` codify every quirk encountered in the OpenEMR Co-Pilot sidecar:

- Patient name walks the array preferring `use === "official"` before falling back to `name[0]`.
- Allergy display falls through `code.text` → `coding[].display` → `coding[].code` → `"Unknown allergen"`.
- Condition prefers ICD-10 over SNOMED CT for the primary code, and carries the source system on the domain object.
- MedicationRequest tolerates `medicationCodeableConcept` OR `medicationReference`, with sane fallbacks for both.
- CareTeam.participant.member is a Reference, populated as `display` in OpenEMR but rarely as a resolved Practitioner.

Each parser is a pure function, typed at both ends. They are the cheapest place to catch FHIR variance without leaking it into rendering.

---

## 4. Trade-offs in the build

| Decision | Trade-off accepted |
|---|---|
| Next.js 16 + RSC | Larger dependency tree than a Svelte- or HTMX-only build; mitigated by `output: "standalone"` keeping the production image at ~120 MB. |
| TypeScript not Swift | Author's stated Swift preference set aside for one-week velocity; revisit if the dashboard expands into long-lived sub-systems. |
| Confidential OIDC client + PKCE | An admin must enable the client once via OpenEMR's admin UI after dynamic registration; documented in `scripts/register-oauth-client.sh` output. |
| `user/*.read` scopes | Clinician sees any chart the OpenEMR ACL allows. The dashboard does not add a second access-control layer; it inherits the EMR's. |
| Encounter as the +1 | Cheaper than Vitals or Labs because Encounter is in the Co-Pilot sidecar's already-validated `DEFAULT_RESOURCE_QUERIES`. Vitals would have added an Observation parser axis that the dashboard does not yet exercise. |
| `Condition?patient=` without filters | OpenEMR's Condition mapper at this version returns HTTP 500 on `category=` and `clinical-status=` filters. Pulling the full bundle and filtering client-side trades a slightly larger response for reliability. |
| Tailwind v4 inline tokens | Dark mode handled via `prefers-color-scheme` rather than a manual switch; clinicians who prefer the opposite of their OS setting cannot override. |
| `output: "standalone"` | Build verifies the standalone directory exists; if `next.config.ts` ever loses the flag the Dockerfile fails with an actionable message rather than a runtime 500. |
| No client-side state for the dashboard | Patient browsing is URL-driven (`/patient/[uuid]`). No client-side router state, no form serialization. Trade: a refresh re-fetches every shard. The browser's HTTP cache + RSC `cache: "no-store"` means clinicians always see live data. |

---

## 5. How to run it

```bash
# 1. OpenEMR up (separate stack — leaves the EMR untouched)
cd ../docker/development-easy && docker compose up --detach --wait

# 2. Provision an OAuth client + write .env.local
cd ../patient-dashboard
bash scripts/register-oauth-client.sh
# (open the admin URL it prints, click "Enable Client")

# 3. Dashboard up
docker compose up --detach --build

# 4. Open the dashboard
open http://localhost:8400/
```

`/healthz` reports a liveness JSON with the build sha so a deploy can be verified at a glance. `/login` is the only unauthenticated route. Every clinical view is gated by middleware and redirects to `/login` for unauthenticated visitors.

---

## 6. What this defense does not claim

- It does not claim feature parity with every legacy dashboard widget. The brief lists five required cards plus one bonus; that is the bar. Billing widgets, insurance, pinned notes, and clinical reminders that the legacy dashboard surfaces are deliberately out of scope. A reviewer asking "why isn't billing here" gets the answer: "billing belongs in a billing view, not a patient summary."
- It does not claim the dashboard replaces OpenEMR. The backend is unmodified. This is a presentation-layer port, exactly as the brief specifies.
- It does not claim the framework choice is universal. Next.js was the right tool for *this* dashboard with *this* timeline. A different team with Elixir muscle memory or a Swift-only org would land elsewhere; the architecture (RSC for streaming, OIDC for auth, FHIR for data, typed validation for boundary safety) is portable across stacks. The framework is the implementation; the principles are the design.

---

## 7. References

- OpenEMR FHIR API: <https://github.com/openemr/openemr/blob/master/FHIR_README.md>
- OpenEMR REST API: <https://github.com/openemr/openemr/blob/master/API_README.md>
- SMART App Launch v2: <https://hl7.org/fhir/smart-app-launch/>
- Auth.js v5 (next-auth): <https://authjs.dev/getting-started/installation>
- Next.js App Router + Server Components: <https://nextjs.org/docs/app>
- Zod (runtime validation): <https://zod.dev/>
- Tailwind CSS v4: <https://tailwindcss.com/docs/installation>
- HL7 FHIR R4 specification: <https://hl7.org/fhir/R4/>
