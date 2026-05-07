/**
 * Domain primitives.
 *
 * The dashboard never lets a raw FHIR resource leak into a UI component.
 * Server components fetch FHIR Bundles, parsers normalise them into the
 * types below, and components render those. This isolates the messy,
 * sparsely-populated real-world FHIR shapes from the rendering layer.
 */

/** A patient's identity row, derived from a Patient resource. */
export type PatientHeader = {
  /** OpenEMR resource UUID (Patient.id). */
  uuid: string;
  /**
   * Medical Record Number — the human-readable identifier surfaced in the
   * legacy OpenEMR header. Sourced from Patient.identifier where
   * system === "...openemr.io/.../mrn" or type.coding[code === "MR"].
   */
  mrn: string | null;
  fullName: string;
  /** Empty when no name resource carries family/given. */
  family: string | null;
  given: string | null;
  /** Sex at birth or administrative gender, whichever the chart records. */
  sex: "male" | "female" | "other" | "unknown" | null;
  /** ISO 8601 date string. Birth-time is never recorded here. */
  birthDate: string | null;
  /**
   * Patient.active. OpenEMR rarely sets this to false but the legacy
   * dashboard exposes it, so we mirror the field even when null.
   */
  active: boolean | null;
};

export type Allergy = {
  id: string;
  /** Display name; falls back through code.coding[].display, code.text, "Unknown". */
  substance: string;
  /** "active" / "inactive" / "resolved" if recorded. */
  clinicalStatus: string | null;
  /** "low" / "high" / "unable-to-assess". */
  criticality: string | null;
  /** Free-text or coded reactions. */
  reactions: string[];
  recordedDate: string | null;
};

export type Problem = {
  id: string;
  label: string;
  /** ICD-10 / SNOMED CT code, whichever is present and most specific. */
  code: string | null;
  codeSystem: "ICD-10" | "SNOMED-CT" | "OTHER" | null;
  clinicalStatus: string | null;
  /** From Condition.category[].coding[].code. */
  category: string | null;
  onsetDate: string | null;
};

export type Medication = {
  id: string;
  label: string;
  /** "active", "completed", "stopped", "on-hold". */
  status: string | null;
  /** "order" (e-prescribed) vs "plan" (problem-list-style). */
  intent: string | null;
  authoredOn: string | null;
  /** First MedicationRequest.dosageInstruction[].text. */
  dosage: string | null;
};

export type Prescription = Medication & {
  /** True for MedicationRequest with intent === "order". */
  prescribed: true;
};

export type CareTeamMember = {
  id: string;
  name: string;
  /** "active" / "inactive" / null. */
  status: string | null;
  /** Display value(s) of CareTeam.participant.role[].text. */
  roles: string[];
};

export type Encounter = {
  id: string;
  /** "ambulatory" / "inpatient" etc. */
  classCode: string | null;
  classDisplay: string | null;
  /** Visit reason — Encounter.reasonCode[0].text. */
  reason: string | null;
  status: string | null;
  start: string | null;
  end: string | null;
};
