/**
 * FHIR resource → domain primitive parsers.
 *
 * Each parser is a pure function. They handle every documented OpenEMR
 * quirk encountered while building the Co-Pilot sidecar:
 *
 * - Patient.name may be empty, single-element with only `text`, or have
 *   `family` only. The parser walks the array prefering `use === "official"`,
 *   then any entry with both family and given, then any entry at all.
 * - Allergies, Problems, Medications: code.coding may be missing entirely.
 *   We fall back through coding[].display → code.text → "Unknown".
 * - Condition.category is an array of CodeableConcept; we read the first
 *   coded category for "problem-list-item" / "encounter-diagnosis".
 * - MedicationRequest may carry medicationCodeableConcept OR
 *   medicationReference (a Reference to a contained Medication). The
 *   reference shape is rarely populated in OpenEMR; we read its
 *   `display` if present, otherwise "Unknown medication".
 * - CareTeam.participant.member is a Reference; OpenEMR populates
 *   `display` but rarely the resolved Practitioner. We use display.
 *
 * Every parser tolerates a missing or malformed resource by returning
 * null. The caller filters nulls out of the rendered list.
 */
import type {
  PatientResourceT,
  AllergyResourceT,
  ConditionResourceT,
  MedicationRequestResourceT,
  CareTeamResourceT,
  EncounterResourceT,
} from "./schemas";
import type {
  PatientHeader,
  Allergy,
  Problem,
  Medication,
  Prescription,
  CareTeamMember,
  Encounter,
} from "./types";

// ---------------------------------------------------------------- patient

export function parsePatient(p: PatientResourceT): PatientHeader {
  const name = pickName(p.name ?? []);
  const family = name?.family ?? null;
  const given = (name?.given ?? []).join(" ") || null;
  const composed = [given, family].filter(Boolean).join(" ").trim();
  const fullName = name?.text ?? (composed || `Patient ${p.id}`);

  return {
    uuid: p.id,
    mrn: pickMrn(p.identifier ?? []),
    fullName,
    family,
    given,
    sex: parseGender(p.gender),
    birthDate: p.birthDate ?? null,
    active: p.active ?? null,
  };
}

function pickName(names: NonNullable<PatientResourceT["name"]>) {
  if (!names.length) return undefined;
  return (
    names.find((n) => n.use === "official" && n.family && n.given?.length) ??
    names.find((n) => n.family && n.given?.length) ??
    names.find((n) => n.text || n.family) ??
    names[0]
  );
}

function pickMrn(idents: NonNullable<PatientResourceT["identifier"]>) {
  // OpenEMR exposes MRN with type.coding[].code === "MR" per US Core. Some
  // installs additionally tag the system; we accept either signal.
  const byType = idents.find((i) =>
    i.type?.coding?.some((c) => c.code === "MR"),
  );
  if (byType?.value) return byType.value;
  const bySystem = idents.find((i) => i.system?.includes("openemr"));
  return bySystem?.value ?? null;
}

function parseGender(g: string | undefined): PatientHeader["sex"] {
  if (g === "male" || g === "female" || g === "other" || g === "unknown") {
    return g;
  }
  return null;
}

// ---------------------------------------------------------------- allergy

export function parseAllergy(a: AllergyResourceT): Allergy {
  const reactions =
    a.reaction
      ?.flatMap((r) =>
        r.manifestation
          ?.map((m) => m.text ?? m.coding?.[0]?.display)
          .filter((s): s is string => Boolean(s)) ?? [],
      )
      .filter((s, i, arr) => arr.indexOf(s) === i) ?? [];

  return {
    id: a.id,
    substance:
      a.code?.text ??
      a.code?.coding?.find((c) => c.display)?.display ??
      a.code?.coding?.[0]?.code ??
      "Unknown allergen",
    clinicalStatus: codeFromCoded(a.clinicalStatus),
    criticality: a.criticality ?? null,
    reactions,
    recordedDate: a.recordedDate ?? null,
  };
}

// ------------------------------------------------------------- condition

export function parseCondition(c: ConditionResourceT): Problem {
  const primaryCoding =
    c.code?.coding?.find((cd) => cd.system?.includes("icd-10")) ??
    c.code?.coding?.find((cd) => cd.system?.includes("snomed")) ??
    c.code?.coding?.[0];

  return {
    id: c.id,
    label:
      c.code?.text ??
      primaryCoding?.display ??
      primaryCoding?.code ??
      "Unspecified problem",
    code: primaryCoding?.code ?? null,
    codeSystem: codeSystemFor(primaryCoding?.system),
    clinicalStatus: codeFromCoded(c.clinicalStatus),
    category: c.category?.[0]?.coding?.[0]?.code ?? null,
    onsetDate: c.onsetDateTime ?? c.onsetPeriod?.start ?? c.recordedDate ?? null,
  };
}

function codeSystemFor(system: string | undefined): Problem["codeSystem"] {
  if (!system) return null;
  if (system.includes("icd-10")) return "ICD-10";
  if (system.includes("snomed")) return "SNOMED-CT";
  return "OTHER";
}

// ------------------------------------------------------ medication request

export function parseMedicationRequest(m: MedicationRequestResourceT): Medication {
  const label =
    m.medicationCodeableConcept?.text ??
    m.medicationCodeableConcept?.coding?.find((c) => c.display)?.display ??
    m.medicationReference?.display ??
    m.medicationCodeableConcept?.coding?.[0]?.code ??
    "Unknown medication";

  return {
    id: m.id,
    label,
    status: m.status ?? null,
    intent: m.intent ?? null,
    authoredOn: m.authoredOn ?? null,
    dosage: m.dosageInstruction?.[0]?.text ?? null,
  };
}

/** Filter for the e-prescribed subset surfaced as the "Prescriptions" card. */
export function isPrescription(m: Medication): m is Prescription {
  return m.intent === "order" || m.intent === "original-order" || m.intent === null;
}

// --------------------------------------------------------------- careteam

export function parseCareTeam(ct: CareTeamResourceT): CareTeamMember[] {
  const members: CareTeamMember[] = [];
  for (const p of ct.participant ?? []) {
    const name =
      p.member?.display ??
      p.member?.reference?.split("/").pop() ??
      "Unknown member";
    const roles =
      p.role
        ?.map((r) => r.text ?? r.coding?.[0]?.display)
        .filter((s): s is string => Boolean(s)) ?? [];
    members.push({
      id: `${ct.id}:${members.length}`,
      name,
      status: ct.status ?? null,
      roles,
    });
  }
  return members;
}

// -------------------------------------------------------------- encounter

export function parseEncounter(e: EncounterResourceT): Encounter {
  return {
    id: e.id,
    classCode: e.class?.code ?? null,
    classDisplay: e.class?.display ?? null,
    reason: e.reasonCode?.[0]?.text ?? e.reasonCode?.[0]?.coding?.[0]?.display ?? null,
    status: e.status ?? null,
    start: e.period?.start ?? null,
    end: e.period?.end ?? null,
  };
}

// ----------------------------------------------------------------- common

function codeFromCoded(
  c: { coding?: { code?: string }[]; text?: string } | undefined,
): string | null {
  return c?.coding?.[0]?.code ?? c?.text ?? null;
}
