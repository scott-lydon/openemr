/**
 * Server-side FHIR client.
 *
 * Reads the access token from the Auth.js session, fans out parallel
 * requests across OpenEMR's per-resource FHIR R4 endpoints, validates
 * each Bundle against a Zod schema, and returns the parsed entries.
 *
 * Why per-resource fan-out and not Patient/$everything: OpenEMR does not
 * implement $everything; the closest equivalents ($docref, $export) are
 * slower than parallel per-resource calls and harder to cache.
 *
 * Why this client lives in a server module only: the bearer token must
 * never reach a client component. The "use server" boundary, plus the
 * `import "server-only"` line below, makes Next.js refuse to bundle this
 * file into a client chunk.
 */
import "server-only";
import { z } from "zod";
import { auth } from "@/auth";
import { fhirBase, fhirVerifyTls } from "@/lib/env";
import {
  AllergyIntoleranceResource,
  BundleSchema,
  CareTeamResource,
  ConditionResource,
  EncounterResource,
  MedicationRequestResource,
  PatientResource,
} from "./schemas";
import {
  isPrescription,
  parseAllergy,
  parseCareTeam,
  parseCondition,
  parseEncounter,
  parseMedicationRequest,
  parsePatient,
} from "./parsers";
import {
  FhirAuthFailure,
  FhirNoSession,
  FhirNotFound,
  FhirTransportFailure,
  FhirUpstreamFailure,
  FhirValidationFailure,
} from "./errors";
import type {
  Allergy,
  CareTeamMember,
  Encounter,
  Medication,
  PatientHeader,
  Prescription,
  Problem,
} from "./types";

// OpenEMR's FHIR endpoint takes ~20s on the first call after a token is
// minted (session lookup + ACL evaluation are slow on cold paths). Once a
// token is warm the same call finishes in ~3-5s. 60s leaves headroom for
// the cold case without making genuine network failures hang.
const FETCH_TIMEOUT_MS = 60_000;

/** Pull the bearer token off the active session, or throw if signed out. */
async function bearerToken(): Promise<string> {
  const session = await auth();
  if (!session?.accessToken) {
    throw new FhirNoSession();
  }
  return session.accessToken;
}

async function fhirGet<T extends z.ZodTypeAny>(
  path: string,
  schema: T,
): Promise<z.infer<T>> {
  const token = await bearerToken();
  const url = new URL(path.replace(/^\/+/, ""), fhirBase).toString();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

  let response: Response;
  try {
    response = await fetch(url, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/fhir+json",
      },
      signal: controller.signal,
      // Each call hits OpenEMR over a fresh request; do NOT cache because
      // the dashboard is supposed to be live.
      cache: "no-store",
      // Honour OPENEMR_FHIR_VERIFY_SSL. Node 20+ respects NODE_TLS_REJECT
      // at process level; surfacing a per-call fetch dispatcher would
      // require undici, which is overkill for dev. The env loader warns
      // when verify is off; production should always have it on.
      ...(fhirVerifyTls ? {} : {}),
    });
  } catch (cause) {
    clearTimeout(timer);
    throw new FhirTransportFailure(url, cause);
  }
  clearTimeout(timer);

  const body = await response.text();

  if (response.status === 404) {
    throw new FhirNotFound(url);
  }
  if (response.status === 401 || response.status === 403) {
    throw new FhirAuthFailure(url, response.status, body);
  }
  if (response.status >= 400) {
    throw new FhirUpstreamFailure(url, response.status, body);
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    throw new FhirUpstreamFailure(
      url,
      response.status,
      `Body was not JSON. First 300 chars: ${body.slice(0, 300)}`,
    );
  }

  const result = schema.safeParse(parsed);
  if (!result.success) {
    throw new FhirValidationFailure(
      url,
      result.error.issues.map((i) => ({ path: i.path, message: i.message })),
    );
  }
  return result.data;
}

/** Drain a Bundle into the array of resources it contains. */
function entries<R>(bundle: { entry?: Array<{ resource?: R }> }): R[] {
  return (bundle.entry ?? [])
    .map((e) => e.resource)
    .filter((r): r is R => r !== undefined);
}

// ============================================================
// Public API — one function per dashboard card.
// ============================================================

/**
 * Resolve a patient identifier into a typed header.
 *
 * Accepts either:
 *   - The FHIR resource UUID (preferred — matches Patient.id)
 *   - A plain string the caller treats as the UUID
 *
 * Queried via `?_id={uuid}` rather than `Patient/{uuid}` so the response
 * is a Bundle, which the parsers can handle uniformly with every other
 * shard. (See clinical-copilot/sidecar/snapshot/fhir_client.py for the
 * empirical reasoning behind this choice.)
 */
export async function fetchPatientHeader(uuid: string): Promise<PatientHeader> {
  const bundle = await fhirGet(
    `Patient?_id=${encodeURIComponent(uuid)}`,
    BundleSchema(PatientResource),
  );
  const [first] = entries(bundle);
  if (!first) {
    throw new FhirNotFound(`Patient?_id=${uuid}`);
  }
  return parsePatient(first);
}

export async function fetchAllergies(uuid: string): Promise<Allergy[]> {
  const bundle = await fhirGet(
    `AllergyIntolerance?patient=${encodeURIComponent(uuid)}`,
    BundleSchema(AllergyIntoleranceResource),
  );
  return entries(bundle).map(parseAllergy);
}

/**
 * Active problem list.
 *
 * Important: we deliberately omit `category=…` and `clinical-status=…`
 * filters. OpenEMR's Condition mapper at this version returns HTTP 500
 * ("SQL Statement failed on preparation") when those filters are
 * present. Pull the full bundle and filter client-side instead.
 */
export async function fetchProblems(uuid: string): Promise<Problem[]> {
  const bundle = await fhirGet(
    `Condition?patient=${encodeURIComponent(uuid)}`,
    BundleSchema(ConditionResource),
  );
  return entries(bundle)
    .map(parseCondition)
    .filter(
      (c) =>
        c.category === "problem-list-item" || c.category === null,
    )
    .filter(
      (c) =>
        c.clinicalStatus === null ||
        c.clinicalStatus === "active" ||
        c.clinicalStatus === "recurrence" ||
        c.clinicalStatus === "relapse",
    );
}

export async function fetchMedications(uuid: string): Promise<Medication[]> {
  const bundle = await fhirGet(
    `MedicationRequest?patient=${encodeURIComponent(uuid)}`,
    BundleSchema(MedicationRequestResource),
  );
  return entries(bundle).map(parseMedicationRequest);
}

export async function fetchPrescriptions(uuid: string): Promise<Prescription[]> {
  const all = await fetchMedications(uuid);
  return all.filter(isPrescription);
}

export async function fetchCareTeam(uuid: string): Promise<CareTeamMember[]> {
  const bundle = await fhirGet(
    `CareTeam?patient=${encodeURIComponent(uuid)}`,
    BundleSchema(CareTeamResource),
  );
  return entries(bundle).flatMap(parseCareTeam);
}

export async function fetchEncounters(uuid: string): Promise<Encounter[]> {
  const bundle = await fhirGet(
    `Encounter?patient=${encodeURIComponent(uuid)}&_sort=-date&_count=20`,
    BundleSchema(EncounterResource),
  );
  return entries(bundle).map(parseEncounter);
}
