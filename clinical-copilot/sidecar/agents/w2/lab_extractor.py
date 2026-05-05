"""Multi-page Vision Language Model (VLM) extractor for lab PDFs.

Per page:

1. **Extract pass.** Send the page image plus its native text to the VLM
   with the lab-extraction prompt. The model returns candidate fields
   under strict JSON schema.
2. **Verify pass.** Send the same page image plus the candidate fields
   from pass 1 to the VLM with the verification prompt. The model
   confirms or rejects each candidate.
3. **Reconcile.** A field confirmed by both passes is kept. A field
   rejected by the verify pass is dropped, with an
   ``ExtractionWarning(code=TWO_PASS_DISAGREEMENT)`` recorded.

Aggregation:

- Each candidate field carries its own ``page`` index in the bbox.
- The top-level ``LabPdfExtraction`` aggregates fields from every page.
- The full ``page_count`` is reported, regardless of how many pages
  produced fields.

Why two passes:

- A single pass on long forms hallucinates fields that look plausible
  but are not actually on the page. The verify pass catches these
  because the verifier sees the candidates as a hypothesis to falsify,
  not text to copy.
- The two-pass cost overhead is one extra forward call per page; the
  hallucination reduction more than pays for it on the eval suite.

Below-floor candidates are dropped at parse time by the lab schema's
``confidence_meets_floor`` validator. A drop emits an
``ExtractionWarning(code=LOW_CONFIDENCE)`` so a clinician can see the
gap rather than silently believing the report is complete.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from sidecar.agents.w2.vlm_client import (
    VlmClient,
    VlmExtractionRequest,
    parse_response_json,
)
from sidecar.ingest.errors import IngestError, _ErrorMeta
from sidecar.ingest.render import PageImage
from sidecar.schemas.w2.extraction_warning import (
    ExtractionWarning,
    ExtractionWarningCode,
)
from sidecar.schemas.w2.lab import (
    LAB_FIELD_CONFIDENCE_FLOOR,
    AbnormalFlag,
    LabPdfExtraction,
    LabResultField,
)


logger = logging.getLogger(__name__)


# Prompt versions are wired into the trace so a regression after a
# prompt edit is attributable. Bumped on any prompt edit.
LAB_EXTRACT_PROMPT_VERSION = "lab.extract.v1"
LAB_VERIFY_PROMPT_VERSION = "lab.verify.v1"


class LabExtractionFailed(IngestError):
    """The VLM produced output the extractor cannot parse safely.

    Happens when the model returns JSON that is structurally valid but
    semantically inconsistent (a field with negative confidence, a bbox
    that overflows the page, etc.). The job goes through the queue's
    retry path; if retries are exhausted, it ends in dead_letter.
    """

    META = _ErrorMeta(
        code="lab_extraction_failed",
        http_status=500,
        debug_hint=(
            "Lab extractor could not parse the VLM response. The raw "
            "response is in the trace as extraction.raw_response. Inspect "
            "for a field that violates the strict Pydantic schema. If the "
            "model regressed, pin to the previous known-good model in the "
            "configuration."
        ),
    )


async def extract_lab_pdf(
    *,
    document_id: str,
    document_sha256: str,
    patient_id: str,
    pages: list[PageImage],
    page_native_text: list[str],
    vlm_client: VlmClient,
) -> LabPdfExtraction:
    """Run the two-pass extractor over every page; aggregate results.

    ``pages`` and ``page_native_text`` must have the same length and be
    in document order. Mismatched lengths raise ``ValueError`` because
    sending the wrong native text to a page risks the model conflating
    pages.
    """
    if len(pages) != len(page_native_text):
        raise ValueError(
            f"page count mismatch: {len(pages)} images vs "
            f"{len(page_native_text)} native-text entries; renderer and "
            "native-text extractor must produce the same number of pages."
        )
    if not pages:
        raise ValueError(
            "extract_lab_pdf received zero pages; the renderer should "
            "raise UploadPdfSanitizationError instead of returning empty."
        )

    all_results: list[LabResultField] = []
    all_warnings: list[ExtractionWarning] = []

    for page in pages:
        native_text = page_native_text[page.page_index]
        try:
            page_results, page_warnings = await _extract_one_page(
                document_id=document_id,
                patient_id=patient_id,
                page=page,
                native_text=native_text,
                vlm_client=vlm_client,
            )
        except LabExtractionFailed:
            raise
        except Exception as exc:
            raise LabExtractionFailed(
                f"page_index={page.page_index} unhandled "
                f"{type(exc).__name__}: {exc!s}"
            ) from exc
        all_results.extend(page_results)
        all_warnings.extend(page_warnings)

    return LabPdfExtraction(
        document_id=document_id,
        document_sha256=document_sha256,
        patient_id=patient_id,
        page_count=len(pages),
        extracted_at=datetime.utcnow(),
        extracted_by_model="composite",
        prompt_version=f"{LAB_EXTRACT_PROMPT_VERSION}+{LAB_VERIFY_PROMPT_VERSION}",
        results=all_results,
        extraction_warnings=all_warnings,
    )


async def _extract_one_page(
    *,
    document_id: str,
    patient_id: str,
    page: PageImage,
    native_text: str,
    vlm_client: VlmClient,
) -> tuple[list[LabResultField], list[ExtractionWarning]]:
    """Two-pass extract for one page. Return (kept fields, warnings)."""
    extract_request = VlmExtractionRequest(
        document_id=document_id,
        patient_id=patient_id,
        page_index=page.page_index,
        page_image_png=page.png_bytes,
        page_native_text=native_text,
        prompt_version=LAB_EXTRACT_PROMPT_VERSION,
        pass_label="extract",
    )
    extract_response = await vlm_client.extract_lab_page(extract_request)
    candidates_json = parse_response_json(extract_response)

    if not isinstance(candidates_json, dict):
        raise LabExtractionFailed(
            f"page_index={page.page_index} extract pass: top-level JSON is "
            f"{type(candidates_json).__name__}, expected object."
        )
    raw_candidates = candidates_json.get("results")
    if not isinstance(raw_candidates, list):
        raise LabExtractionFailed(
            f"page_index={page.page_index} extract pass: "
            "'results' is missing or not a list."
        )

    candidates: list[LabResultField] = []
    warnings: list[ExtractionWarning] = []

    for raw in raw_candidates:
        parsed = _parse_candidate(
            raw=raw,
            page_index=page.page_index,
            warnings=warnings,
        )
        if parsed is not None:
            candidates.append(parsed)

    if not candidates:
        return [], warnings

    # ── Verify pass ──────────────────────────────────────────────────
    verify_request = VlmExtractionRequest(
        document_id=document_id,
        patient_id=patient_id,
        page_index=page.page_index,
        page_image_png=page.png_bytes,
        # Send the candidates as the "context" half of the prompt rather
        # than mutating native_text. Models read structured JSON in the
        # context cleaner than ad-hoc text concatenation.
        page_native_text=json.dumps(
            {"candidates": [c.model_dump(mode="json") for c in candidates]},
            default=str,
        ),
        prompt_version=LAB_VERIFY_PROMPT_VERSION,
        pass_label="verify",
    )
    verify_response = await vlm_client.extract_lab_page(verify_request)
    verify_json = parse_response_json(verify_response)
    if not isinstance(verify_json, dict):
        raise LabExtractionFailed(
            f"page_index={page.page_index} verify pass: top-level JSON is "
            f"{type(verify_json).__name__}, expected object."
        )
    verdicts = verify_json.get("verdicts")
    if not isinstance(verdicts, list) or len(verdicts) != len(candidates):
        raise LabExtractionFailed(
            f"page_index={page.page_index} verify pass: 'verdicts' must be "
            f"a list of length {len(candidates)}, got "
            f"{type(verdicts).__name__}({len(verdicts) if isinstance(verdicts, list) else '?'})."
        )

    kept: list[LabResultField] = []
    for candidate, verdict in zip(candidates, verdicts):
        if not isinstance(verdict, dict):
            warnings.append(
                ExtractionWarning(
                    code=ExtractionWarningCode.SCHEMA_VIOLATION,
                    message=(
                        f"verify pass returned non-object verdict for "
                        f"{candidate.test_name!r}; field dropped."
                    ),
                    field=candidate.test_name,
                )
            )
            continue
        if bool(verdict.get("confirmed")):
            kept.append(candidate)
        else:
            warnings.append(
                ExtractionWarning(
                    code=ExtractionWarningCode.TWO_PASS_DISAGREEMENT,
                    message=(
                        f"verify pass rejected {candidate.test_name!r}: "
                        f"{verdict.get('reason', 'no reason given')!s}"
                    ),
                    field=candidate.test_name,
                )
            )
    return kept, warnings


def _parse_candidate(
    *,
    raw: object,
    page_index: int,
    warnings: list[ExtractionWarning],
) -> LabResultField | None:
    """Parse one candidate dict into ``LabResultField``.

    Drop-with-warning is the contract: a candidate whose confidence is
    below the floor is dropped and a ``LOW_CONFIDENCE`` warning is added
    to ``warnings``. A schema violation also drops with a
    ``SCHEMA_VIOLATION`` warning. Only confirmed-valid fields return a
    parsed model.
    """
    if not isinstance(raw, dict):
        warnings.append(
            ExtractionWarning(
                code=ExtractionWarningCode.SCHEMA_VIOLATION,
                message=f"non-object candidate on page {page_index}; dropped.",
                field=None,
            )
        )
        return None

    raw_dict: dict[str, Any] = dict(raw)
    confidence = raw_dict.get("confidence")
    if isinstance(confidence, (int, float)) and confidence < LAB_FIELD_CONFIDENCE_FLOOR:
        warnings.append(
            ExtractionWarning(
                code=ExtractionWarningCode.LOW_CONFIDENCE,
                message=(
                    f"confidence={confidence:.3f} below floor "
                    f"{LAB_FIELD_CONFIDENCE_FLOOR:.2f} for "
                    f"test_name={raw_dict.get('test_name')!r}"
                ),
                field=str(raw_dict.get("test_name", "unknown")),
            )
        )
        return None

    raw_dict.setdefault("abnormal_flag", AbnormalFlag.UNKNOWN.value)
    try:
        # ``strict=False`` lets the wire-format payload coerce strings to
        # enums (e.g. "H" -> AbnormalFlag.HIGH) and integers to floats
        # where the schema permits. The schema still enforces strict
        # mode for in-process construction; this only relaxes the wire
        # boundary, which is exactly the right place to coerce.
        return LabResultField.model_validate(raw_dict, strict=False)
    except ValidationError as exc:
        warnings.append(
            ExtractionWarning(
                code=ExtractionWarningCode.SCHEMA_VIOLATION,
                message=(
                    f"Pydantic rejected candidate on page {page_index}: "
                    f"{exc.errors()[:3]!r}"
                ),
                field=str(raw_dict.get("test_name", "unknown")),
            )
        )
        return None


__all__ = [
    "LAB_EXTRACT_PROMPT_VERSION",
    "LAB_VERIFY_PROMPT_VERSION",
    "LabExtractionFailed",
    "extract_lab_pdf",
]
