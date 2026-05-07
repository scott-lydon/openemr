/**
 * Zod schemas for the FHIR R4 resources the dashboard reads.
 *
 * The schemas are deliberately *loose*: every field that varies in real
 * OpenEMR responses is `.optional()` or `.nullable()`. Strict shape
 * enforcement is the parsers' job. The schemas exist to:
 *   1. Reject responses that aren't FHIR Bundles at all (e.g. an HTML
 *      error page that snuck through with status 200).
 *   2. Give the parsers a typed surface to read so domain primitives
 *      don't accidentally consume a numeric `id` as a string.
 *
 * If OpenEMR's mapper changes shape, the validation failure includes the
 * exact path so the cause is obvious.
 */
import { z } from "zod";

// ---------------------------------------------------------------- common

const Coding = z
  .object({
    system: z.string().optional(),
    code: z.string().optional(),
    display: z.string().optional(),
  })
  .passthrough();

const CodeableConcept = z
  .object({
    text: z.string().optional(),
    coding: z.array(Coding).optional(),
  })
  .passthrough();

const Period = z
  .object({
    start: z.string().optional(),
    end: z.string().optional(),
  })
  .passthrough();

const Reference = z
  .object({
    reference: z.string().optional(),
    display: z.string().optional(),
  })
  .passthrough();

const Identifier = z
  .object({
    system: z.string().optional(),
    value: z.string().optional(),
    type: CodeableConcept.optional(),
  })
  .passthrough();

const HumanName = z
  .object({
    use: z.string().optional(),
    text: z.string().optional(),
    family: z.string().optional(),
    given: z.array(z.string()).optional(),
    prefix: z.array(z.string()).optional(),
    suffix: z.array(z.string()).optional(),
  })
  .passthrough();

// ---------------------------------------------------------------- bundle

/**
 * Generic Bundle wrapper. We avoid `.strict()` because OpenEMR sometimes
 * adds extension fields the spec allows. Anything we don't read is silently
 * passed through.
 */
export function BundleSchema<T extends z.ZodTypeAny>(entryResource: T) {
  return z.object({
    resourceType: z.literal("Bundle"),
    type: z.string().optional(),
    total: z.number().optional(),
    entry: z
      .array(
        z
          .object({
            resource: entryResource.optional(),
          })
          .passthrough(),
      )
      .optional(),
  });
}

// ---------------------------------------------------------------- patient

export const PatientResource = z
  .object({
    resourceType: z.literal("Patient"),
    id: z.string(),
    active: z.boolean().nullable().optional(),
    identifier: z.array(Identifier).optional(),
    name: z.array(HumanName).optional(),
    gender: z.string().optional(),
    birthDate: z.string().optional(),
  })
  .passthrough();

// -------------------------------------------------------------- allergy

export const AllergyIntoleranceResource = z
  .object({
    resourceType: z.literal("AllergyIntolerance"),
    id: z.string(),
    clinicalStatus: CodeableConcept.optional(),
    verificationStatus: CodeableConcept.optional(),
    criticality: z.string().optional(),
    code: CodeableConcept.optional(),
    recordedDate: z.string().optional(),
    reaction: z
      .array(
        z
          .object({
            manifestation: z.array(CodeableConcept).optional(),
            description: z.string().optional(),
          })
          .passthrough(),
      )
      .optional(),
  })
  .passthrough();

// ------------------------------------------------------------ condition

export const ConditionResource = z
  .object({
    resourceType: z.literal("Condition"),
    id: z.string(),
    clinicalStatus: CodeableConcept.optional(),
    verificationStatus: CodeableConcept.optional(),
    category: z.array(CodeableConcept).optional(),
    code: CodeableConcept.optional(),
    onsetDateTime: z.string().optional(),
    onsetPeriod: Period.optional(),
    recordedDate: z.string().optional(),
  })
  .passthrough();

// ------------------------------------------------------ medication request

export const MedicationRequestResource = z
  .object({
    resourceType: z.literal("MedicationRequest"),
    id: z.string(),
    status: z.string().optional(),
    intent: z.string().optional(),
    medicationCodeableConcept: CodeableConcept.optional(),
    medicationReference: Reference.optional(),
    authoredOn: z.string().optional(),
    dosageInstruction: z
      .array(
        z
          .object({
            text: z.string().optional(),
          })
          .passthrough(),
      )
      .optional(),
  })
  .passthrough();

// --------------------------------------------------------------- careteam

export const CareTeamResource = z
  .object({
    resourceType: z.literal("CareTeam"),
    id: z.string(),
    status: z.string().optional(),
    name: z.string().optional(),
    participant: z
      .array(
        z
          .object({
            member: Reference.optional(),
            role: z.array(CodeableConcept).optional(),
          })
          .passthrough(),
      )
      .optional(),
  })
  .passthrough();

// -------------------------------------------------------------- encounter

export const EncounterResource = z
  .object({
    resourceType: z.literal("Encounter"),
    id: z.string(),
    status: z.string().optional(),
    class: Coding.optional(),
    period: Period.optional(),
    reasonCode: z.array(CodeableConcept).optional(),
  })
  .passthrough();

// ----------------------------------------------------------- exports

export type Bundle<T> = {
  resourceType: "Bundle";
  type?: string;
  total?: number;
  entry?: Array<{ resource?: T }>;
};

export type PatientResourceT = z.infer<typeof PatientResource>;
export type AllergyResourceT = z.infer<typeof AllergyIntoleranceResource>;
export type ConditionResourceT = z.infer<typeof ConditionResource>;
export type MedicationRequestResourceT = z.infer<typeof MedicationRequestResource>;
export type CareTeamResourceT = z.infer<typeof CareTeamResource>;
export type EncounterResourceT = z.infer<typeof EncounterResource>;
