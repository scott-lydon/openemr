"""HL7 FHIR R4 DocumentReference create.

Wraps an ``httpx`` client around the OpenEMR FHIR endpoint to write a
``DocumentReference`` resource per upload. The resource carries the
sanitized bytes inline (base64), the SHA-256 hash, the document type,
and the patient reference.

Why a typed wrapper:

- The FHIR client is the only network call the upload pipeline makes
  to OpenEMR. Mocking the network at this seam (rather than at the
  generic ``httpx`` boundary) keeps tests focused on the ingest contract.
- Errors from OpenEMR vary widely in shape (4xx with OperationOutcome,
  5xx with HTML, network failure with RequestError). The wrapper
  collapses them into ``UploadFhirWriteError`` with enough detail to
  triage.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

import httpx

from sidecar.ingest.errors import UploadFhirWriteError
from sidecar.ingest.types import AcceptedMimeType, DocType


@dataclass(frozen=True)
class FhirDocumentRefRequest:
    """Inputs needed to create a ``DocumentReference`` resource.

    The patient_id is the OpenEMR FHIR identifier (``Patient/<uuid>``).
    The MIME type and document type drive the ``content[0].attachment``
    and ``type.coding`` fields.
    """

    patient_id: str
    doc_type: DocType
    mime_type: AcceptedMimeType
    sanitized_bytes: bytes
    sha256_hex: str


@dataclass(frozen=True)
class FhirDocumentRefResponse:
    """The FHIR server's confirmation of a successful create.

    ``document_id`` is the resource id assigned by the server. The
    upload pipeline returns this id to the BFF as the canonical handle.
    """

    document_id: str
    raw_status: int
    raw_location: str | None


class FhirDocumentRefClient(Protocol):
    """Protocol for the DocumentReference create call.

    Production implementation calls OpenEMR's FHIR R4 API. Tests inject
    a stub that records inputs and returns deterministic responses.
    """

    async def create(self, request: FhirDocumentRefRequest) -> FhirDocumentRefResponse:
        ...


class HttpxFhirClient:
    """``httpx``-backed implementation."""

    def __init__(
        self,
        *,
        fhir_base: str,
        access_token: str,
        verify_ssl: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._fhir_base = fhir_base.rstrip("/")
        self._access_token = access_token
        self._verify_ssl = verify_ssl
        self._timeout = httpx.Timeout(timeout_seconds)

    async def create(self, request: FhirDocumentRefRequest) -> FhirDocumentRefResponse:
        body = self._build_resource(request)

        try:
            async with httpx.AsyncClient(
                verify=self._verify_ssl, timeout=self._timeout
            ) as client:
                response = await client.post(
                    f"{self._fhir_base}/DocumentReference",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/fhir+json",
                        "Content-Type": "application/fhir+json",
                    },
                )
        except httpx.RequestError as exc:
            raise UploadFhirWriteError(
                f"network error talking to OpenEMR FHIR: "
                f"{type(exc).__name__}: {exc!s}"
            ) from exc

        if response.status_code not in {200, 201}:
            raise UploadFhirWriteError(
                f"OpenEMR FHIR DocumentReference returned status="
                f"{response.status_code}; body[:512]="
                f"{response.text[:512]!r}"
            )

        # Two locations to look for the assigned id: the JSON ``id`` field
        # if the body echoes the resource, and the ``Location`` header
        # otherwise. Try both; raise if neither contains a valid id.
        document_id = self._extract_id_from_response(response)
        return FhirDocumentRefResponse(
            document_id=document_id,
            raw_status=response.status_code,
            raw_location=response.headers.get("Location"),
        )

    def _build_resource(self, request: FhirDocumentRefRequest) -> dict[str, object]:
        encoded = base64.b64encode(request.sanitized_bytes).decode("ascii")
        return {
            "resourceType": "DocumentReference",
            "status": "current",
            "type": {
                "coding": [
                    {
                        "system": "http://hl7.org/fhir/document-reference-type",
                        "code": _doc_type_loinc_code(request.doc_type),
                        "display": request.doc_type.value,
                    }
                ]
            },
            "subject": {"reference": request.patient_id},
            "content": [
                {
                    "attachment": {
                        "contentType": request.mime_type.value,
                        "data": encoded,
                        "hash": request.sha256_hex,
                    }
                }
            ],
        }

    def _extract_id_from_response(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            id_value = payload.get("id")
            if isinstance(id_value, str) and id_value:
                return id_value
        location = response.headers.get("Location") or ""
        # FHIR Location header looks like
        #   ".../DocumentReference/<id>/_history/<vid>"
        if "/DocumentReference/" in location:
            tail = location.split("/DocumentReference/", 1)[1]
            head = tail.split("/", 1)[0]
            if head:
                return head
        raise UploadFhirWriteError(
            "OpenEMR FHIR returned 2xx but no document id was found in "
            "the response body or Location header."
        )


def _doc_type_loinc_code(doc_type: DocType) -> str:
    """Map our internal doc_type to a Logical Observation Identifiers
    Names and Codes (LOINC) code for the DocumentReference.type coding.

    These codes are the closest matches in the LOINC document ontology.
    A clinician documentation system would refine these against a real
    LOINC value set; for the prototype the mapping is sufficient for
    downstream filtering.
    """
    return {
        DocType.LAB_PDF: "11502-2",        # Laboratory report
        DocType.INTAKE_FORM: "57598-9",    # Intake history (proxy)
        DocType.REFERRAL_FAX: "57133-1",   # Referral note
    }[doc_type]


class StubFhirClient:
    """Deterministic stub used by unit tests.

    The stub stores every request it received in ``self.requests`` so a
    test can assert on the request shape without touching httpx at all.
    """

    def __init__(self, document_id: str = "stub-document-id") -> None:
        self.document_id = document_id
        self.requests: list[FhirDocumentRefRequest] = []

    async def create(self, request: FhirDocumentRefRequest) -> FhirDocumentRefResponse:
        self.requests.append(request)
        return FhirDocumentRefResponse(
            document_id=self.document_id,
            raw_status=201,
            raw_location=f"/DocumentReference/{self.document_id}",
        )


__all__ = [
    "FhirDocumentRefClient",
    "FhirDocumentRefRequest",
    "FhirDocumentRefResponse",
    "HttpxFhirClient",
    "StubFhirClient",
]
