"""Multi-page Vision Language Model (VLM) extractor for intake forms.

Mirrors ``lab_extractor.py``: each page is rendered, sent through a
two-pass extract-then-verify pipeline, and the surviving fields land
in ``IntakeFormExtraction``.

The intake schema differs from the lab schema:

- Demographics block is exactly one. The extractor merges per-page
  demographics by taking the highest-confidence non-empty values.
- Medications, allergies, family history are lists, like lab results.
- Chief concern is free text; the extractor concatenates per-page
  chief-concern values with ``\n\n`` separators.

Confidence floor for intake is 0.75 (vs 0.7 for labs) because intake
allergies and medications are clinically more dangerous when wrong.
"""

from __future__ import annotations

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
from sidecar.schemas.w2.intake import (
    INTAKE_FIELD_CONFIDENCE_FLOOR,
    AllergyField,
    DemographicsBlock,
    FamilyHistoryField,
    IntakeFormExtraction,
    MedicationField,
    SexCode,
)


logger = logging.getLogger(__name__)


INTAKE_EXTRACT_PROMPT_VERSION = "intake.extract.v1"
INTAKE_VERIFY_PROMPT_VERSION = "intake.verify.v1"


class IntakeExtractionFailed(IngestError):
    META = _ErrorMeta(
        code="intake_extraction_failed",
        http_status=500,
        debug_hint=(
            "Intake extractor could not parse the VLM response. The raw "
            "response is in the trace. Inspect the rejected fields and "
            "consider pinning the previous prompt version."
        ),
    )


async def extract_intake_pdf(
    *,
    document_id: str,
    document_sha256: str,
    patient_id: str,
    pages: list[PageImage],
    page_native_text: list[str],
    vlm_client: VlmClient,
) -> IntakeFormExtraction:
    """Run the two-pass extractor over every page; aggregate."""
    if len(pages) != len(page_native_text):
        raise ValueError(
            f"page count mismatch: {len(pages)} images vs "
            f"{len(page_native_text)} native-text entries."
        )
    if not pages:
        raise ValueError("extract_intake_pdf received zero pages.")

    medications: list[MedicationField] = []
    allergies: list[AllergyField] = []
    family_history: list[FamilyHistoryField] = []
    chief_concern_chunks: list[str] = []
    demographics_candidates: list[DemographicsBlock] = []
    warnings: list[ExtractionWarning] = []

    for page in pages:
        try:
            page_result = await _extract_one_page(
                document_id=document_id,
                patient_id=patient_id,
                page=page,
                native_text=page_native_text[page.page_index],
                vlm_client=vlm_client,
            )
        except IntakeExtractionFailed:
            raise
        except Exception as exc:
            raise IntakeExtractionFailed(
                f"page_index={page.page_index} unhandled "
                f"{type(exc).__name__}: {exc!s}"
            ) from exc

        medications.extend(page_result["medications"])
        allergies.extend(page_result["allergies"])
        family_history.extend(page_result["family_history"])
        warnings.extend(page_result["warnings"])
        chief = page_result.get("chief_concern")
        if chief:
            chief_concern_chunks.append(str(chief))
        demo = page_result.get("demographics")
        if isinstance(demo, DemographicsBlock):
            demographics_candidates.append(demo)

    demographics = _merge_demographics(demographics_candidates)

    return IntakeFormExtraction(
        document_id=document_id,
        document_sha256=document_sha256,
        patient_id=patient_id,
        page_count=len(pages),
        extracted_at=datetime.utcnow(),
        extracted_by_model="composite",
        prompt_version=f"{INTAKE_EXTRACT_PROMPT_VERSION}+{INTAKE_VERIFY_PROMPT_VERSION}",
        demographics=demographics,
        chief_concern="\n\n".join(chief_concern_chunks) or None,
        current_medications=medications,
        allergies=allergies,
        family_history=family_history,
        extraction_warnings=warnings,
    )


async def _extract_one_page(
    *,
    document_id: str,
    patient_id: str,
    page: PageImage,
    native_text: str,
    vlm_client: VlmClient,
) -> dict[str, Any]:
    """One page of two-pass intake extraction.

    Returns a dict with the per-page fields aggregated by the caller.
    Using a dict (not a dataclass) here because the page result is a
    short-lived assembly artifact, not a domain object.
    """
    extract_request = VlmExtractionRequest(
        document_id=document_id,
        patient_id=patient_id,
        page_index=page.page_index,
        page_image_png=page.png_bytes,
        page_native_text=native_text,
        prompt_version=INTAKE_EXTRACT_PROMPT_VERSION,
        pass_label="extract",
    )
    extract_response = await vlm_client.extract_intake_page(extract_request)
    candidates = parse_response_json(extract_response)
    if not isinstance(candidates, dict):
        raise IntakeExtractionFailed(
            f"page_index={page.page_index} extract pass: top-level JSON is "
            f"{type(candidates).__name__}, expected object."
        )

    medications = _parse_anchored_list(
        candidates.get("medications") or [],
        ctor=MedicationField,
        page_index=page.page_index,
        warnings_into=(warnings := []),
    )
    allergies = _parse_anchored_list(
        candidates.get("allergies") or [],
        ctor=AllergyField,
        page_index=page.page_index,
        warnings_into=warnings,
    )
    family_history = _parse_anchored_list(
        candidates.get("family_history") or [],
        ctor=FamilyHistoryField,
        page_index=page.page_index,
        warnings_into=warnings,
    )
    demographics = _parse_demographics(candidates.get("demographics"))
    chief_concern = candidates.get("chief_concern")

    # Verify pass: confirm the high-stakes fields (medications + allergies).
    # Demographics and family history are lower stakes and skip the verify
    # pass to keep cost and latency bounded.
    confirmed_medications = await _two_pass_filter(
        items=medications,
        page=page,
        document_id=document_id,
        patient_id=patient_id,
        category="medications",
        vlm_client=vlm_client,
        warnings=warnings,
        name_of=lambda m: m.name,
    )
    confirmed_allergies = await _two_pass_filter(
        items=allergies,
        page=page,
        document_id=document_id,
        patient_id=patient_id,
        category="allergies",
        vlm_client=vlm_client,
        warnings=warnings,
        name_of=lambda a: a.substance,
    )

    return {
        "medications": confirmed_medications,
        "allergies": confirmed_allergies,
        "family_history": family_history,
        "chief_concern": chief_concern,
        "demographics": demographics,
        "warnings": warnings,
    }


async def _two_pass_filter(
    *,
    items: list[Any],
    page: PageImage,
    document_id: str,
    patient_id: str,
    category: str,
    vlm_client: VlmClient,
    warnings: list[ExtractionWarning],
    name_of,
) -> list[Any]:
    """Run the verify pass against ``items``; drop those rejected.

    Empty input bypasses the verify call entirely so a page with no
    medications does not pay for a no-op model call.
    """
    if not items:
        return []
    import json

    verify_request = VlmExtractionRequest(
        document_id=document_id,
        patient_id=patient_id,
        page_index=page.page_index,
        page_image_png=page.png_bytes,
        page_native_text=json.dumps(
            {category: [item.model_dump(mode="json") for item in items]},
            default=str,
        ),
        prompt_version=INTAKE_VERIFY_PROMPT_VERSION,
        pass_label="verify",
    )
    response = await vlm_client.extract_intake_page(verify_request)
    verdict_json = parse_response_json(response)
    if not isinstance(verdict_json, dict):
        raise IntakeExtractionFailed(
            f"page_index={page.page_index} verify pass for {category}: "
            f"top-level JSON is {type(verdict_json).__name__}, expected object."
        )
    verdicts = verdict_json.get("verdicts")
    if not isinstance(verdicts, list) or len(verdicts) != len(items):
        raise IntakeExtractionFailed(
            f"page_index={page.page_index} verify pass for {category}: "
            f"'verdicts' must be list of length {len(items)}, got "
            f"{type(verdicts).__name__}({len(verdicts) if isinstance(verdicts, list) else '?'})."
        )

    confirmed: list[Any] = []
    for item, verdict in zip(items, verdicts):
        if isinstance(verdict, dict) and bool(verdict.get("confirmed")):
            confirmed.append(item)
        else:
            reason = (
                verdict.get("reason", "no reason given")
                if isinstance(verdict, dict)
                else "non-object verdict"
            )
            warnings.append(
                ExtractionWarning(
                    code=ExtractionWarningCode.TWO_PASS_DISAGREEMENT,
                    message=(
                        f"verify pass rejected {category[:-1]} "
                        f"{name_of(item)!r} on page {page.page_index}: {reason!s}"
                    ),
                    field=str(name_of(item)),
                )
            )
    return confirmed


def _parse_anchored_list(
    raw_items: list[Any],
    *,
    ctor,
    page_index: int,
    warnings_into: list[ExtractionWarning],
) -> list[Any]:
    """Parse a list of dicts into ``ctor`` instances; drop with warning."""
    parsed: list[Any] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            warnings_into.append(
                ExtractionWarning(
                    code=ExtractionWarningCode.SCHEMA_VIOLATION,
                    message=f"non-object item on page {page_index}; dropped.",
                    field=None,
                )
            )
            continue
        confidence = raw.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < INTAKE_FIELD_CONFIDENCE_FLOOR:
            warnings_into.append(
                ExtractionWarning(
                    code=ExtractionWarningCode.LOW_CONFIDENCE,
                    message=(
                        f"confidence={confidence:.3f} below floor "
                        f"{INTAKE_FIELD_CONFIDENCE_FLOOR:.2f} on page "
                        f"{page_index}; dropped."
                    ),
                    field=str(_first_id_field(raw)),
                )
            )
            continue
        try:
            # ``strict=False`` coerces wire-format strings to enums and
            # ints to floats. See lab_extractor for full rationale.
            parsed.append(ctor.model_validate(raw, strict=False))
        except ValidationError as exc:
            warnings_into.append(
                ExtractionWarning(
                    code=ExtractionWarningCode.SCHEMA_VIOLATION,
                    message=(
                        f"Pydantic rejected {ctor.__name__} on page "
                        f"{page_index}: {exc.errors()[:3]!r}"
                    ),
                    field=str(_first_id_field(raw)),
                )
            )
    return parsed


def _parse_demographics(raw: Any) -> DemographicsBlock | None:
    """Parse the demographics block; ``None`` if absent or malformed.

    Demographics has a softer contract than medications/allergies because
    a missing demographic field is a gap, not a danger. We never raise
    here; we return ``None`` and let the merge step decide.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return DemographicsBlock.model_validate(raw, strict=False)
    except ValidationError:
        return None


def _merge_demographics(candidates: list[DemographicsBlock]) -> DemographicsBlock:
    """Merge per-page demographics by picking the highest-confidence values.

    A field unset on every page returns ``None`` (or the appropriate
    schema default). This is intentionally lossy: we do not invent
    demographic values that the document did not provide.
    """
    if not candidates:
        return DemographicsBlock(confidence=0.0, source_quote="")

    best_name: str | None = None
    best_dob = None
    best_sex = SexCode.UNKNOWN
    best_confidence = 0.0
    best_quote = ""
    best_bbox = None

    for cand in candidates:
        if cand.full_name and cand.confidence >= best_confidence:
            best_name = cand.full_name
            best_quote = cand.source_quote or best_quote
        if cand.dob and best_dob is None:
            best_dob = cand.dob
        if cand.sex is not SexCode.UNKNOWN and best_sex is SexCode.UNKNOWN:
            best_sex = cand.sex
        best_confidence = max(best_confidence, cand.confidence)
        best_bbox = best_bbox or cand.bbox

    return DemographicsBlock(
        full_name=best_name,
        dob=best_dob,
        sex=best_sex,
        confidence=best_confidence,
        source_quote=best_quote,
        bbox=best_bbox,
    )


def _first_id_field(raw: dict[str, Any]) -> str:
    """Pick a meaningful identifier from a candidate dict for warning text."""
    for key in ("name", "substance", "relation", "test_name"):
        if isinstance(raw.get(key), str):
            return raw[key]
    return "unknown"


__all__ = [
    "INTAKE_EXTRACT_PROMPT_VERSION",
    "INTAKE_VERIFY_PROMPT_VERSION",
    "IntakeExtractionFailed",
    "extract_intake_pdf",
]
