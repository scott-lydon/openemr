# Clinician Guide — Clinical Co-Pilot

Audience: the doctor or nurse practitioner using the launch button.

## What this is

The Clinical Co-Pilot is a second-opinion assistant for the patient in front of you. It does not diagnose, prescribe, or treat. It produces ranked options and citations to the chart so you can decide faster, with the source visible.

You are responsible for every decision. If the AI is wrong, that responsibility does not transfer to it.

## What this is not

- It is **not** a replacement for your clinical judgment.
- It is **not** an autonomous agent. It cannot order labs, write prescriptions, or close encounters.
- It is **not** a search engine. It only reads from the patient's chart and a small set of curated guidelines.

## How to use it

### Use case A — Diagnostic cross-check

1. Open the patient's summary page in OpenEMR.
2. Click `Clinical Co-Pilot (AI)`.
3. Type something like: `What are the top 3 most likely diagnoses given this chart?`
4. Read the ranked answer. Each diagnosis includes a citation to a specific FHIR resource (a Condition, a MedicationRequest, an Observation, etc.).
5. Click any citation to see the underlying chart entry.

### Use case B — Chart-error scan

1. Click `Clinical Co-Pilot (AI)`.
2. Type: `Are there any chart inconsistencies I should review?`
3. The answer is a short list of flagged items. Each one says what is inconsistent and why. Common findings:
   - An active medication with a contraindication listed in the patient's allergies.
   - A medication active for a condition that is no longer in the active problem list.
   - A lab value that has not been re-checked since a date the protocol says it should have been.

### Use case C — Follow-up question

The chat surface accepts freeform questions about the patient in front of you. The AI is scoped to that patient's chart; it cannot answer questions about other patients.

Example questions:

- `Has this patient had a colonoscopy in the last 10 years?`
- `What did the cardiologist note say about the murmur in 2024?`
- `Summarize the medication changes since the last visit.`

### Use case D — Document ingest

1. Drag a PDF (lab report, consult note, discharge summary) into the chat.
2. The AI extracts structured fields and shows you a preview.
3. You confirm or correct each field.
4. On confirm, the structured data is written back to the patient's chart via FHIR.

This use case requires the Pro plan.

## What the citations mean

Every claim the AI makes about the patient is anchored to a specific FHIR resource in the chart. The citation tells you:

- The resource type (Condition, MedicationRequest, etc.).
- The resource id.
- A direct link to the resource in OpenEMR.

If a claim has **no citation**, the AI says so explicitly and refuses to use it as evidence. You should treat un-cited claims as if they were not said.

## When to override

Override the AI when:

- It cites a resource that has been retracted or marked entered-in-error.
- It assumes a guideline applies that doesn't match the patient's history (e.g. a screening recommendation for the wrong age band).
- The reasoning chain reaches a conclusion that conflicts with your direct clinical observation of the patient.

Overriding is one click — close the tab and proceed as you normally would. The audit log records that the conversation happened; it does not lock you into any particular action.

## When to escalate to a human

- The AI surfaces a finding you don't understand. Send the conversation transcript to a peer or a specialist.
- The AI flags an inconsistency that involves a wrong medication. Stop and verify the prescription history before any action.
- The AI's confidence is high but its citations don't actually support the conclusion. This is the most important escalation. Report it via the in-chat feedback button so it goes into the evaluation set for the next model release.

## Limits to know about

- The AI does not see anything outside the patient's chart. It cannot access lab values from a different facility, family history that isn't documented, or imaging that isn't in OpenEMR.
- It is rate-limited per clinician (Starter plan: 30 chats/hour; Pro plan: 200 chats/hour). Hard rate limits prevent runaway-loop scenarios.
- It does not work without an internet connection (the sidecar talks to OpenAI).

## Feedback

Every chat has a `thumbs-up / thumbs-down / wrong` button at the bottom. Use it. Thumbs-down feedback goes into the next evaluation cycle and directly improves the prompts.

## Privacy

Your chats stay within the BAA chain. OpenAI processes the chat under their Enterprise BAA with Zero-Data-Retention (no chat content is retained on their side). Nothing crosses to a vendor without a BAA in place.

The chat is logged for seven (7) years in a hash-chained audit log. The audit log is for compliance only — it cannot be re-read out of context to second-guess a clinical decision. See the operator's BAA for details.
