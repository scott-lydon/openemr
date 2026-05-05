"""Persist extracted clinical data back to OpenEMR Fast Healthcare
Interoperability Resources (FHIR) Release 4.

Maps:

- ``LabResultField`` → ``Observation`` plus ``Provenance`` pointing back
  to the source ``DocumentReference``.
- ``MedicationField`` (intake) → ``MedicationStatement``.
- ``AllergyField`` (intake) → ``AllergyIntolerance``.
- ``IntakeFormExtraction.chief_concern`` plus ``family_history`` →
  ``QuestionnaireResponse``.

Deterministic resource identifiers:

- Every persisted resource has an ``id`` derived from
  ``sha256(document_id + page + field_id)``. Re-running the extractor
  against the same document produces the same set of resource ids, so
  the upload pipeline is idempotent at the resource level — a
  re-extraction does not duplicate.
- The hash is truncated to 32 hex characters to fit FHIR id constraints
  (max 64 chars, alphanumeric plus ``.-``). 128 bits of hash entropy is
  more than enough to make collisions astronomically unlikely.

Why a separate module:

- The wire shape of a ``MedicationStatement`` is unrelated to the wire
  shape of a ``Observation``; building each in its own pure function
  keeps both readable.
- The persist module is the only seam that talks to OpenEMR's FHIR
  server in the write direction during extraction. Mocking this seam
  lets unit tests verify the resource shape without standing up a
  server.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from sidecar.ingest.errors import IngestError, _ErrorMeta
from sidecar.schemas.w2.intake import (
    AllergyField,
    IntakeFormExtraction,
    MedicationField,
)
from sidecar.schemas.w2.lab import LabPdfExtraction, LabResultField


logger = logging.getLogger(__name__)


class FhirPersistError(IngestError):
    """The FHIR write-back call failed.

    Distinct from ``UploadFhirWriteError`` (the upload-time
    ``DocumentReference`` create) because the failure modes differ:
    here the document is already stored, and we are decorating it with
    extracted fields. A failed write does not invalidate the document;
    the runbook entry for §3.2 tells the operator to retry the
    extraction job.
    """

    META = _ErrorMeta(
        code="fhir_persist_failed",
        http_status=502,
        debug_hint=(
            "FHIR Observation/Medication/Allergy write-back failed. The "
            "DocumentReference is already stored, so the source bytes are "
            "preserved. The job will retry per the queue's exponential "
            "backoff. Check OpenEMR FHIR server health and OAuth token "
            "validity in the trace."
        ),
    )


# ─── Deterministic identifiers ────────────────────────────────────────


def deterministic_resource_id(
    *,
    document_id: str,
    page_index: int,
    field_id: str,
) -> str:
    """Compute a stable FHIR resource id for ``(document, page, field)``.

    The id is the first 32 hex characters of
    ``sha256(document_id + ':' + page + ':' + field_id)``. The
    delimiters prevent ``("AB", "C")`` and ``("A", "BC")`` collisions
    that a naive concatenation would produce.

    Returns a string that satisfies FHIR's resource id grammar:
    32 lowercase hex characters; no dot or dash needed; safe in URLs and
    in path components.
    """
    if not document_id:
        raise ValueError("document_id must be non-empty for deterministic id")
    if page_index < 0:
        raise ValueError(f"page_index must be >= 0, got {page_index}")
    if not field_id:
        raise ValueError("field_id must be non-empty for deterministic id")
    digest = hashlib.sha256(
        f"{document_id}:{page_index}:{field_id}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


# ─── FHIR resource builders ───────────────────────────────────────────


def lab_field_to_observation(
    *,
    document_id: str,
    patient_id: str,
    field: LabResultField,
) -> dict[str, Any]:
    """Build a FHIR R4 ``Observation`` resource for one lab field.

    The ``derivedFrom`` reference points back to the source document so
    the chart UI can deep-link to the citation preview. The
    ``identifier`` carries the deterministic id as a system-scoped
    business identifier so a future re-extraction overwrites cleanly.
    """
    page_index = field.bbox.page if field.bbox is not None else 0
    field_id = field.test_name
    rid = deterministic_resource_id(
        document_id=document_id,
        page_index=page_index,
        field_id=field_id,
    )
    obs: dict[str, Any] = {
        "resourceType": "Observation",
        "id": rid,
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": (
                            "http://terminology.hl7.org/CodeSystem/"
                            "observation-category"
                        ),
                        "code": "laboratory",
                        "display": "Laboratory",
                    }
                ]
            }
        ],
        "code": _build_code(field),
        "subject": {"reference": patient_id},
        "valueQuantity": _build_value_quantity(field),
        "interpretation": [
            {
                "coding": [
                    {
                        "system": (
                            "http://terminology.hl7.org/CodeSystem/v3-"
                            "ObservationInterpretation"
                        ),
                        "code": field.abnormal_flag.value,
                    }
                ]
            }
        ] if field.abnormal_flag.value != "unknown" else [],
        "referenceRange": _build_reference_range(field),
        "derivedFrom": [{"reference": f"DocumentReference/{document_id}"}],
        "identifier": [
            {
                "system": "https://clinical-copilot.openemr/extraction-id",
                "value": rid,
            }
        ],
        "extension": [
            {
                "url": "https://clinical-copilot.openemr/extraction/confidence",
                "valueDecimal": field.confidence,
            },
        ],
    }
    if field.collection_date is not None:
        obs["effectiveDateTime"] = field.collection_date.isoformat()
    return obs


def lab_observation_to_provenance(
    *,
    observation: dict[str, Any],
    document_id: str,
) -> dict[str, Any]:
    """Build a Provenance resource that ties an Observation to its source.

    Required by §4.4 of the build plan. The Provenance is the auditable
    record that says "this Observation came from this DocumentReference,
    extracted at this time, by this model"; the chart UI uses it to
    show users the trail.
    """
    obs_id = observation["id"]
    return {
        "resourceType": "Provenance",
        "id": deterministic_resource_id(
            document_id=document_id,
            page_index=0,
            field_id=f"prov:Observation/{obs_id}",
        ),
        "target": [{"reference": f"Observation/{obs_id}"}],
        "recorded": datetime.utcnow().isoformat() + "Z",
        "agent": [
            {
                "type": {
                    "coding": [
                        {
                            "system": (
                                "http://terminology.hl7.org/CodeSystem/"
                                "provenance-participant-type"
                            ),
                            "code": "assembler",
                        }
                    ]
                },
                "who": {
                    "reference": "Device/clinical-copilot-extractor",
                },
            }
        ],
        "entity": [
            {
                "role": "source",
                "what": {"reference": f"DocumentReference/{document_id}"},
            }
        ],
    }


def medication_field_to_statement(
    *,
    document_id: str,
    patient_id: str,
    field: MedicationField,
) -> dict[str, Any]:
    """Build a FHIR R4 ``MedicationStatement`` from an intake medication."""
    page_index = field.bbox.page if field.bbox is not None else 0
    field_id = f"medication:{field.name}:{field.dose or ''}"
    rid = deterministic_resource_id(
        document_id=document_id,
        page_index=page_index,
        field_id=field_id,
    )
    statement: dict[str, Any] = {
        "resourceType": "MedicationStatement",
        "id": rid,
        "status": "active",
        "subject": {"reference": patient_id},
        "medicationCodeableConcept": {
            "coding": _maybe_rxnorm_coding(field),
            "text": field.name,
        },
        "derivedFrom": [{"reference": f"DocumentReference/{document_id}"}],
        "identifier": [
            {
                "system": "https://clinical-copilot.openemr/extraction-id",
                "value": rid,
            }
        ],
        "extension": [
            {
                "url": "https://clinical-copilot.openemr/extraction/confidence",
                "valueDecimal": field.confidence,
            },
        ],
    }
    if field.dose or field.frequency:
        statement["dosage"] = [
            {
                "text": ", ".join(
                    component for component in (field.dose, field.frequency)
                    if component
                )
            }
        ]
    return statement


def allergy_field_to_resource(
    *,
    document_id: str,
    patient_id: str,
    field: AllergyField,
) -> dict[str, Any]:
    """Build a FHIR R4 ``AllergyIntolerance`` from an intake allergy."""
    page_index = field.bbox.page if field.bbox is not None else 0
    field_id = f"allergy:{field.substance}"
    rid = deterministic_resource_id(
        document_id=document_id,
        page_index=page_index,
        field_id=field_id,
    )
    resource: dict[str, Any] = {
        "resourceType": "AllergyIntolerance",
        "id": rid,
        "clinicalStatus": {
            "coding": [
                {
                    "system": (
                        "http://terminology.hl7.org/CodeSystem/"
                        "allergyintolerance-clinical"
                    ),
                    "code": "active",
                }
            ]
        },
        "verificationStatus": {
            "coding": [
                {
                    "system": (
                        "http://terminology.hl7.org/CodeSystem/"
                        "allergyintolerance-verification"
                    ),
                    "code": "unconfirmed",
                }
            ]
        },
        "code": {"text": field.substance},
        "patient": {"reference": patient_id},
        "identifier": [
            {
                "system": "https://clinical-copilot.openemr/extraction-id",
                "value": rid,
            }
        ],
        "extension": [
            {
                "url": "https://clinical-copilot.openemr/extraction/confidence",
                "valueDecimal": field.confidence,
            },
        ],
    }
    if field.reaction or field.severity:
        manifestation = [{"text": field.reaction}] if field.reaction else []
        resource["reaction"] = [
            {
                "manifestation": manifestation,
                "severity": _coerce_severity(field.severity),
            }
        ]
    return resource


def intake_to_questionnaire_response(
    *,
    extraction: IntakeFormExtraction,
) -> dict[str, Any]:
    """Build a FHIR R4 ``QuestionnaireResponse`` for the free-text intake.

    Captures chief concern and family history. Medications and allergies
    have their own resources; this resource is the catch-all for free
    text the chart does not have a structured slot for.
    """
    items: list[dict[str, Any]] = []
    if extraction.chief_concern:
        items.append(
            {
                "linkId": "chief_concern",
                "text": "Chief concern",
                "answer": [{"valueString": extraction.chief_concern}],
            }
        )
    for fh in extraction.family_history:
        items.append(
            {
                "linkId": f"family_history:{fh.relation}:{fh.condition}",
                "text": (
                    f"Family history — {fh.relation} with {fh.condition}"
                    + (
                        f" (diagnosed at {fh.age_at_diagnosis})"
                        if fh.age_at_diagnosis is not None
                        else ""
                    )
                ),
                "answer": [{"valueString": fh.source_quote}],
            }
        )

    rid = deterministic_resource_id(
        document_id=extraction.document_id,
        page_index=0,
        field_id="intake:questionnaire_response",
    )
    return {
        "resourceType": "QuestionnaireResponse",
        "id": rid,
        "status": "completed",
        "subject": {"reference": extraction.patient_id},
        "authored": extraction.extracted_at.isoformat(),
        "identifier": {
            "system": "https://clinical-copilot.openemr/extraction-id",
            "value": rid,
        },
        "item": items,
    }


# ─── Persistence orchestrator ─────────────────────────────────────────


@dataclass(frozen=True)
class FhirPersistRequest:
    """A bundle of FHIR resources to write for one extraction."""

    resources: list[dict[str, Any]]


@dataclass(frozen=True)
class FhirPersistResponse:
    """Confirmation of which resources were upserted."""

    resource_ids: list[str]


class FhirPersistClient(Protocol):
    """Protocol for the persistence call.

    Production implementation issues a FHIR transaction Bundle (PUT
    by id, so the upsert is idempotent). Tests substitute a stub.
    """

    async def upsert_bundle(self, request: FhirPersistRequest) -> FhirPersistResponse:
        ...


@dataclass
class StubFhirPersistClient:
    """Records every bundle for inspection by tests."""

    bundles: list[FhirPersistRequest] = field(default_factory=list)

    async def upsert_bundle(self, request: FhirPersistRequest) -> FhirPersistResponse:
        self.bundles.append(request)
        return FhirPersistResponse(
            resource_ids=[
                str(resource.get("id", "")) for resource in request.resources
            ]
        )


class HttpxFhirPersistClient:
    """``httpx``-backed transaction Bundle writer."""

    def __init__(
        self,
        *,
        fhir_base: str,
        access_token: str,
        verify_ssl: bool = True,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._fhir_base = fhir_base.rstrip("/")
        self._access_token = access_token
        self._verify_ssl = verify_ssl
        self._timeout = httpx.Timeout(timeout_seconds)

    async def upsert_bundle(self, request: FhirPersistRequest) -> FhirPersistResponse:
        if not request.resources:
            return FhirPersistResponse(resource_ids=[])

        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": resource,
                    "request": {
                        "method": "PUT",
                        "url": f"{resource['resourceType']}/{resource['id']}",
                    },
                }
                for resource in request.resources
            ],
        }
        try:
            async with httpx.AsyncClient(
                verify=self._verify_ssl, timeout=self._timeout
            ) as client:
                response = await client.post(
                    self._fhir_base,
                    json=bundle,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/fhir+json",
                        "Content-Type": "application/fhir+json",
                    },
                )
        except httpx.RequestError as exc:
            raise FhirPersistError(
                f"network error writing FHIR Bundle: "
                f"{type(exc).__name__}: {exc!s}"
            ) from exc

        if response.status_code not in {200, 201}:
            raise FhirPersistError(
                f"FHIR Bundle upsert returned status={response.status_code} "
                f"body[:512]={response.text[:512]!r}"
            )
        return FhirPersistResponse(
            resource_ids=[r["id"] for r in request.resources]
        )


# ─── Extraction-shaped persist functions ──────────────────────────────


async def persist_lab_extraction(
    *,
    extraction: LabPdfExtraction,
    client: FhirPersistClient,
) -> list[str]:
    """Write Observations + Provenance for every result in ``extraction``.

    Returns the list of FHIR resource ids that were upserted, in
    document order. The caller logs this list as a span attribute so
    a later re-extraction's diff against the previous list is
    auditable.
    """
    resources: list[dict[str, Any]] = []
    for field in extraction.results:
        observation = lab_field_to_observation(
            document_id=extraction.document_id,
            patient_id=extraction.patient_id,
            field=field,
        )
        provenance = lab_observation_to_provenance(
            observation=observation,
            document_id=extraction.document_id,
        )
        resources.append(observation)
        resources.append(provenance)

    response = await client.upsert_bundle(FhirPersistRequest(resources=resources))
    return response.resource_ids


async def persist_intake_extraction(
    *,
    extraction: IntakeFormExtraction,
    client: FhirPersistClient,
) -> list[str]:
    """Write Medications + Allergies + QuestionnaireResponse."""
    resources: list[dict[str, Any]] = []
    for med in extraction.current_medications:
        resources.append(
            medication_field_to_statement(
                document_id=extraction.document_id,
                patient_id=extraction.patient_id,
                field=med,
            )
        )
    for allergy in extraction.allergies:
        resources.append(
            allergy_field_to_resource(
                document_id=extraction.document_id,
                patient_id=extraction.patient_id,
                field=allergy,
            )
        )
    if extraction.chief_concern or extraction.family_history:
        resources.append(intake_to_questionnaire_response(extraction=extraction))

    response = await client.upsert_bundle(FhirPersistRequest(resources=resources))
    return response.resource_ids


# ─── Internal helpers ─────────────────────────────────────────────────


def _build_code(field: LabResultField) -> dict[str, Any]:
    coding: list[dict[str, str]] = []
    if field.loinc_code:
        coding.append(
            {
                "system": "http://loinc.org",
                "code": field.loinc_code,
                "display": field.test_name,
            }
        )
    return {"coding": coding, "text": field.test_name}


def _build_value_quantity(field: LabResultField) -> dict[str, Any]:
    if field.value_numeric is None:
        return {"value": 0, "unit": field.unit or "", "comparator": "<"}
    return {
        "value": field.value_numeric,
        "unit": field.unit or "",
        "system": "http://unitsofmeasure.org",
    }


def _build_reference_range(field: LabResultField) -> list[dict[str, Any]]:
    if field.reference_range_low is None and field.reference_range_high is None:
        return []
    range_dict: dict[str, Any] = {}
    if field.reference_range_low is not None:
        range_dict["low"] = {"value": field.reference_range_low, "unit": field.unit or ""}
    if field.reference_range_high is not None:
        range_dict["high"] = {"value": field.reference_range_high, "unit": field.unit or ""}
    return [range_dict]


def _maybe_rxnorm_coding(field: MedicationField) -> list[dict[str, str]]:
    if not field.rxnorm_code:
        return []
    return [
        {
            "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
            "code": field.rxnorm_code,
            "display": field.name,
        }
    ]


def _coerce_severity(severity: str | None) -> str:
    """Map free-text severity to FHIR's enum (mild/moderate/severe).

    Anything we cannot map deterministically returns ``"moderate"`` as a
    safe default. The downstream UI shows the raw string in the
    citation card alongside the FHIR-mapped severity.
    """
    if not severity:
        return "moderate"
    lower = severity.strip().lower()
    if "mild" in lower or "minor" in lower:
        return "mild"
    if "severe" in lower or "anaphyl" in lower or "life" in lower:
        return "severe"
    return "moderate"


__all__ = [
    "FhirPersistClient",
    "FhirPersistError",
    "FhirPersistRequest",
    "FhirPersistResponse",
    "HttpxFhirPersistClient",
    "StubFhirPersistClient",
    "allergy_field_to_resource",
    "deterministic_resource_id",
    "intake_to_questionnaire_response",
    "lab_field_to_observation",
    "lab_observation_to_provenance",
    "medication_field_to_statement",
    "persist_intake_extraction",
    "persist_lab_extraction",
]
