"""FHIR-backed ``DocumentSource`` for the ingest worker.

The Phase 2 worker leases a ``QueuedJob`` that carries a
``document_id`` but not the bytes. This module fetches those bytes
back from OpenEMR's FHIR ``DocumentReference`` resource so the
extractor can render and analyse them.

Wire-format:

- ``GET /fhir/DocumentReference/{id}`` returns a JSON resource with
  ``content[0].attachment.data`` populated when the upload pipeline
  inlined the bytes (which it always does).
- The ``data`` field is base64-encoded per FHIR R4.

Why a separate module:

- Keeps ``HttpxFhirClient`` (write path) decoupled from the read path.
  The two endpoints share a base URL and an access token but otherwise
  have nothing in common; merging them would force every test of the
  write path to also stub the read path.
- The protocol surface (``fetch(document_id) -> bytes``) is exactly the
  shape ``extract_dispatcher.DocumentSource`` expects, so this module
  drops in without an adapter.
"""

from __future__ import annotations

import base64
import logging
from typing import Final

import httpx

from sidecar.agents.w2.extract_dispatcher import (
    DocumentSource,
    DocumentSourceError,
)


logger = logging.getLogger(__name__)


# How long to wait on OpenEMR's FHIR endpoint. The DocumentReference
# read is a single GET; even on a busy box it should return within a
# couple of seconds. We give it 30 to absorb cold-start and TLS-handshake
# latency on first call after deploy.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0


class FhirDocumentSource:
    """Read sanitized PDF bytes from a FHIR DocumentReference.

    Construction parameters mirror the write client so the same
    ``settings.openemr_fhir_base`` + ``settings.openemr_access_token``
    config drives both directions of FHIR traffic.
    """

    def __init__(
        self,
        *,
        fhir_base: str,
        access_token: str,
        verify_ssl: bool = True,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not fhir_base:
            raise ValueError(
                "FhirDocumentSource: fhir_base must be non-empty. "
                "Set COPILOT_OPENEMR_FHIR_BASE in .env."
            )
        if not access_token:
            raise ValueError(
                "FhirDocumentSource: access_token must be non-empty. "
                "The worker uses settings.openemr_access_token; if it is "
                "blank the worker cannot read DocumentReference resources."
            )
        self._fhir_base = fhir_base.rstrip("/")
        self._access_token = access_token
        self._verify_ssl = verify_ssl
        self._timeout = httpx.Timeout(timeout_seconds)

    async def fetch(self, document_id: str) -> bytes:
        """GET the resource and decode ``content[0].attachment.data``.

        Raises ``DocumentSourceError`` for every failure mode (network,
        4xx/5xx, missing data field, malformed base64). The error
        message names the document_id and the FHIR base so the operator
        can replay the call manually with curl.
        """
        if not document_id:
            raise DocumentSourceError(
                "FhirDocumentSource.fetch called with empty document_id"
            )
        url = f"{self._fhir_base}/DocumentReference/{document_id}"
        try:
            async with httpx.AsyncClient(
                verify=self._verify_ssl, timeout=self._timeout
            ) as client:
                response = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Accept": "application/fhir+json",
                    },
                )
        except httpx.RequestError as exc:
            raise DocumentSourceError(
                f"network error reading DocumentReference/{document_id} from "
                f"{self._fhir_base}: {type(exc).__name__}: {exc!s}. "
                "Check the FHIR base URL is reachable from inside the "
                "sidecar container and that TLS verification is configured "
                "for the deployment (FHIR_VERIFY_SSL)."
            ) from exc

        if response.status_code != 200:
            raise DocumentSourceError(
                f"OpenEMR FHIR returned status={response.status_code} for "
                f"DocumentReference/{document_id}; "
                f"body[:512]={response.text[:512]!r}. "
                "401/403 means the access_token expired or lacks the "
                "DocumentReference.read scope. 404 means the resource was "
                "deleted between upload and lease — safe to dead-letter."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise DocumentSourceError(
                f"DocumentReference/{document_id} returned non-JSON body: "
                f"{type(exc).__name__}: {exc!s}; "
                f"body[:512]={response.text[:512]!r}."
            ) from exc

        content = payload.get("content")
        if not isinstance(content, list) or not content:
            raise DocumentSourceError(
                f"DocumentReference/{document_id} has no content[]; the "
                "upload path must inline the bytes via "
                "content[0].attachment.data. Re-check the upload route's "
                "DocumentReference build."
            )
        attachment = content[0].get("attachment") if isinstance(content[0], dict) else None
        if not isinstance(attachment, dict):
            raise DocumentSourceError(
                f"DocumentReference/{document_id} content[0].attachment "
                "is missing or not an object."
            )
        data_b64 = attachment.get("data")
        if not isinstance(data_b64, str) or not data_b64:
            raise DocumentSourceError(
                f"DocumentReference/{document_id} content[0].attachment.data "
                "is missing or empty. The bytes must be inlined as base64; "
                "the worker cannot follow content[0].attachment.url."
            )
        try:
            return base64.b64decode(data_b64, validate=False)
        except Exception as exc:
            raise DocumentSourceError(
                f"DocumentReference/{document_id} content[0].attachment.data "
                f"is not valid base64: {type(exc).__name__}: {exc!s}."
            ) from exc


__all__ = ["FhirDocumentSource"]
