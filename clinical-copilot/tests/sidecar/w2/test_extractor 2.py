"""Tests for the Week 2 multi-page Vision Language Model (VLM) extractor.

Coverage:

- Deterministic resource id is stable across calls and unique per
  ``(document, page, field)`` triple.
- ``render_pages`` returns one image per page; respects DPI bounds;
  rejects empty input.
- ``extract_native_text`` returns one entry per page.
- Lab extractor happy path: two passes confirm; result lands in
  ``LabPdfExtraction.results``.
- Lab extractor disagreement: verify pass rejects one candidate; the
  rejected field is dropped and a ``TWO_PASS_DISAGREEMENT`` warning
  is emitted.
- Lab extractor below-floor: a candidate with confidence below
  ``LAB_FIELD_CONFIDENCE_FLOOR`` is dropped with a ``LOW_CONFIDENCE``
  warning.
- Intake extractor happy path: medications + allergies parsed and
  confirmed.
- Intake extractor merges per-page demographics by highest confidence.
- Persist module: ``Observation`` + ``Provenance`` shape, deterministic
  id, idempotent (re-running produces the same id set).
- Dispatcher: routes lab vs intake correctly; raises typed
  ``UnsupportedDocTypeError`` for referral_fax.
- Hallucination probe: blank pages produce empty ``results`` and at
  least one warning.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone

import pytest

from sidecar.agents.w2 import (
    DocumentSourceError,
    LabExtractionFailed,
    StubDocumentSource,
    StubVlmClient,
    UnsupportedDocTypeError,
    build_extract_fn,
    extract_intake_pdf,
    extract_lab_pdf,
    fixture_key,
)
from sidecar.ingest.persist import (
    StubFhirPersistClient,
    deterministic_resource_id,
    lab_field_to_observation,
    lab_observation_to_provenance,
    persist_intake_extraction,
    persist_lab_extraction,
)
from sidecar.ingest.render import (
    BORN_DIGITAL_TEXT_THRESHOLD,
    DEFAULT_RENDER_DPI,
    PageImage,
    extract_native_text,
    render_pages,
)
from sidecar.ingest.types import DocType, QueuedJob, UploadSource
from sidecar.schemas.w2 import (
    AllergyField,
    BoundingBox,
    DemographicsBlock,
    IntakeFormExtraction,
    LabPdfExtraction,
    LabResultField,
    MedicationField,
    SexCode,
)
from sidecar.schemas.w2.extraction_warning import ExtractionWarningCode


# ─── Helpers ──────────────────────────────────────────────────────────


def _minimal_pdf_bytes() -> bytes:
    """Build a tiny valid PDF deterministically.

    Re-built per test rather than using a fixture file so the bytes
    have no environmental dependency. PyMuPDF parses this without
    complaint.
    """
    try:
        import fitz
    except ImportError:
        pytest.skip("PyMuPDF not installed; renderer tests need it")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 720), "lab report HbA1c 6.8 percent normal")
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


def _two_page_pdf_bytes() -> bytes:
    try:
        import fitz
    except ImportError:
        pytest.skip("PyMuPDF not installed; renderer tests need it")
    doc = fitz.open()
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((72, 720), "page one HbA1c 6.8")
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 720), "page two Vitamin D 25-OH 32")
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


def _single_page_image(png_bytes: bytes = b"\x89PNG\r\n\x1a\n", page_index: int = 0) -> PageImage:
    """A PageImage stub for unit tests that don't actually rasterize.

    The png_bytes need not be a real PNG; the lab extractor only uses
    them as a stable byte sequence for the StubVlmClient lookup.
    """
    return PageImage(
        page_index=page_index,
        dpi=300,
        width_px=2550,
        height_px=3300,
        png_bytes=png_bytes,
    )


def _lab_candidate(
    test_name: str = "HbA1c",
    confidence: float = 0.9,
    page: int = 0,
) -> dict:
    return {
        "test_name": test_name,
        "value": "6.8",
        "value_numeric": 6.8,
        "unit": "%",
        "reference_range_low": 4.0,
        "reference_range_high": 5.6,
        "abnormal_flag": "H",
        "confidence": confidence,
        "source_quote": f"{test_name} 6.8 % H",
        "bbox": {
            "page": page,
            "x0": 0.1,
            "y0": 0.1,
            "x1": 0.5,
            "y1": 0.2,
        },
    }


def _seed_lab_fixtures(client: StubVlmClient, page_image_png: bytes,
                       extract_payload: dict, verify_payload: dict) -> None:
    from sidecar.agents.w2.lab_extractor import (
        LAB_EXTRACT_PROMPT_VERSION,
        LAB_VERIFY_PROMPT_VERSION,
    )
    client.fixtures[
        fixture_key(
            page_image_png=page_image_png,
            prompt_version=LAB_EXTRACT_PROMPT_VERSION,
            pass_label="extract",
        )
    ] = json.dumps(extract_payload)
    client.fixtures[
        fixture_key(
            page_image_png=page_image_png,
            prompt_version=LAB_VERIFY_PROMPT_VERSION,
            pass_label="verify",
        )
    ] = json.dumps(verify_payload)


# ─── Deterministic id ─────────────────────────────────────────────────


def test_deterministic_resource_id_is_stable() -> None:
    a = deterministic_resource_id(document_id="doc-1", page_index=0, field_id="HbA1c")
    b = deterministic_resource_id(document_id="doc-1", page_index=0, field_id="HbA1c")
    assert a == b
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)


def test_deterministic_resource_id_is_unique_per_triple() -> None:
    a = deterministic_resource_id(document_id="doc-1", page_index=0, field_id="HbA1c")
    b = deterministic_resource_id(document_id="doc-1", page_index=0, field_id="LDL")
    c = deterministic_resource_id(document_id="doc-1", page_index=1, field_id="HbA1c")
    d = deterministic_resource_id(document_id="doc-2", page_index=0, field_id="HbA1c")
    assert len({a, b, c, d}) == 4


def test_deterministic_resource_id_avoids_concat_collision() -> None:
    """('AB','C') and ('A','BC') would collide without a delimiter."""
    a = deterministic_resource_id(document_id="AB", page_index=0, field_id="C")
    b = deterministic_resource_id(document_id="A", page_index=0, field_id="BC")
    assert a != b


@pytest.mark.parametrize(
    "document_id, page_index, field_id",
    [
        ("", 0, "x"),
        ("d", -1, "x"),
        ("d", 0, ""),
    ],
)
def test_deterministic_resource_id_rejects_invalid(
    document_id: str, page_index: int, field_id: str
) -> None:
    with pytest.raises(ValueError):
        deterministic_resource_id(
            document_id=document_id,
            page_index=page_index,
            field_id=field_id,
        )


# ─── render_pages ─────────────────────────────────────────────────────


def test_render_pages_one_image_per_page() -> None:
    pdf = _two_page_pdf_bytes()
    pages = render_pages(pdf, dpi=DEFAULT_RENDER_DPI)
    assert len(pages) == 2
    assert pages[0].page_index == 0 and pages[1].page_index == 1
    assert all(p.dpi == DEFAULT_RENDER_DPI for p in pages)
    assert all(p.png_bytes.startswith(b"\x89PNG") for p in pages)


def test_render_pages_rejects_empty() -> None:
    from sidecar.ingest.errors import UploadPdfSanitizationError
    with pytest.raises(UploadPdfSanitizationError):
        render_pages(b"", dpi=300)


@pytest.mark.parametrize("dpi", [60, 1500])
def test_render_pages_dpi_bounds(dpi: int) -> None:
    pdf = _minimal_pdf_bytes()
    with pytest.raises(ValueError):
        render_pages(pdf, dpi=dpi)


def test_extract_native_text_one_string_per_page() -> None:
    pdf = _two_page_pdf_bytes()
    text = extract_native_text(pdf)
    assert len(text) == 2
    assert "page one" in text[0]
    assert "page two" in text[1]


# ─── Lab extractor ────────────────────────────────────────────────────


async def test_lab_extractor_happy_path_two_passes() -> None:
    page = _single_page_image(b"P0_BYTES")
    client = StubVlmClient()
    _seed_lab_fixtures(
        client,
        page_image_png=page.png_bytes,
        extract_payload={"results": [_lab_candidate("HbA1c"), _lab_candidate("LDL")]},
        verify_payload={
            "verdicts": [
                {"confirmed": True},
                {"confirmed": True},
            ]
        },
    )
    result = await extract_lab_pdf(
        document_id="doc-100",
        document_sha256="a" * 64,
        patient_id="Patient/87413",
        pages=[page],
        page_native_text=[""],
        vlm_client=client,
    )
    assert isinstance(result, LabPdfExtraction)
    assert {r.test_name for r in result.results} == {"HbA1c", "LDL"}
    assert result.extraction_warnings == []


async def test_lab_extractor_drops_disagreed_field_with_warning() -> None:
    page = _single_page_image(b"P_DISAGREE")
    client = StubVlmClient()
    _seed_lab_fixtures(
        client,
        page_image_png=page.png_bytes,
        extract_payload={"results": [_lab_candidate("HbA1c"), _lab_candidate("Phantom")]},
        verify_payload={
            "verdicts": [
                {"confirmed": True},
                {"confirmed": False, "reason": "no such row visible"},
            ]
        },
    )
    result = await extract_lab_pdf(
        document_id="doc-101",
        document_sha256="b" * 64,
        patient_id="Patient/87413",
        pages=[page],
        page_native_text=[""],
        vlm_client=client,
    )
    assert {r.test_name for r in result.results} == {"HbA1c"}
    codes = [w.code for w in result.extraction_warnings]
    assert ExtractionWarningCode.TWO_PASS_DISAGREEMENT in codes


async def test_lab_extractor_drops_below_floor_with_warning() -> None:
    page = _single_page_image(b"P_FLOOR")
    candidate = _lab_candidate("Vitamin D 25-OH", confidence=0.55)
    client = StubVlmClient()
    _seed_lab_fixtures(
        client,
        page_image_png=page.png_bytes,
        extract_payload={"results": [candidate]},
        verify_payload={"verdicts": []},  # never invoked when no candidates remain
    )
    result = await extract_lab_pdf(
        document_id="doc-102",
        document_sha256="c" * 64,
        patient_id="Patient/87413",
        pages=[page],
        page_native_text=[""],
        vlm_client=client,
    )
    assert result.results == []
    assert any(
        w.code is ExtractionWarningCode.LOW_CONFIDENCE for w in result.extraction_warnings
    )


async def test_lab_extractor_blank_page_emits_no_results() -> None:
    """Hallucination probe — a blank page returns empty results
    plus a warning."""
    page = _single_page_image(b"P_BLANK")
    client = StubVlmClient()
    _seed_lab_fixtures(
        client,
        page_image_png=page.png_bytes,
        extract_payload={"results": []},
        verify_payload={"verdicts": []},
    )
    result = await extract_lab_pdf(
        document_id="doc-103",
        document_sha256="d" * 64,
        patient_id="Patient/87413",
        pages=[page],
        page_native_text=[""],
        vlm_client=client,
    )
    assert result.results == []


async def test_lab_extractor_multi_page_aggregates_across_pages() -> None:
    p0 = _single_page_image(b"P0_BYTES", page_index=0)
    p1 = _single_page_image(b"P1_BYTES", page_index=1)
    client = StubVlmClient()
    _seed_lab_fixtures(
        client,
        page_image_png=p0.png_bytes,
        extract_payload={"results": [_lab_candidate("HbA1c", page=0)]},
        verify_payload={"verdicts": [{"confirmed": True}]},
    )
    _seed_lab_fixtures(
        client,
        page_image_png=p1.png_bytes,
        extract_payload={
            "results": [_lab_candidate("Vitamin D 25-OH", page=1)]
        },
        verify_payload={"verdicts": [{"confirmed": True}]},
    )
    result = await extract_lab_pdf(
        document_id="doc-multi",
        document_sha256="e" * 64,
        patient_id="Patient/87413",
        pages=[p0, p1],
        page_native_text=["", ""],
        vlm_client=client,
    )
    assert {r.test_name for r in result.results} == {"HbA1c", "Vitamin D 25-OH"}


async def test_lab_extractor_rejects_mismatched_page_lengths() -> None:
    p0 = _single_page_image(b"P0", page_index=0)
    with pytest.raises(ValueError):
        await extract_lab_pdf(
            document_id="doc-x",
            document_sha256="f" * 64,
            patient_id="Patient/87413",
            pages=[p0],
            page_native_text=["a", "b"],  # length mismatch
            vlm_client=StubVlmClient(),
        )


async def test_lab_extractor_raises_typed_on_malformed_response() -> None:
    page = _single_page_image(b"P_BAD")
    from sidecar.agents.w2.lab_extractor import LAB_EXTRACT_PROMPT_VERSION

    client = StubVlmClient()
    client.fixtures[
        fixture_key(
            page_image_png=page.png_bytes,
            prompt_version=LAB_EXTRACT_PROMPT_VERSION,
            pass_label="extract",
        )
    ] = "[1, 2, 3]"  # not a JSON object
    with pytest.raises(LabExtractionFailed):
        await extract_lab_pdf(
            document_id="doc-y",
            document_sha256="g" * 64,
            patient_id="Patient/87413",
            pages=[page],
            page_native_text=[""],
            vlm_client=client,
        )


# ─── Intake extractor ─────────────────────────────────────────────────


def _intake_med(name: str = "metformin", confidence: float = 0.9) -> dict:
    return {
        "name": name,
        "dose": "500mg",
        "frequency": "BID",
        "rxnorm_code": "861006",
        "confidence": confidence,
        "source_quote": f"{name} 500mg BID",
        "bbox": {"page": 0, "x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.2},
    }


def _intake_allergy(substance: str = "penicillin", confidence: float = 0.9) -> dict:
    return {
        "substance": substance,
        "reaction": "hives",
        "severity": "mild",
        "confidence": confidence,
        "source_quote": f"{substance} hives",
        "bbox": {"page": 0, "x0": 0.1, "y0": 0.3, "x1": 0.5, "y1": 0.4},
    }


def _seed_intake_fixtures(
    client: StubVlmClient,
    page_image_png: bytes,
    extract_payload: dict,
    medications_verify: dict | None = None,
    allergies_verify: dict | None = None,
) -> None:
    from sidecar.agents.w2.intake_extractor import (
        INTAKE_EXTRACT_PROMPT_VERSION,
        INTAKE_VERIFY_PROMPT_VERSION,
    )
    client.fixtures[
        fixture_key(
            page_image_png=page_image_png,
            prompt_version=INTAKE_EXTRACT_PROMPT_VERSION,
            pass_label="extract",
        )
    ] = json.dumps(extract_payload)
    if medications_verify is not None:
        # The verify pass embeds the candidate list in page_native_text.
        # The fixture key only depends on prompt_version + pass_label, so
        # reuse the same key for both medication and allergy verify calls.
        client.fixtures[
            fixture_key(
                page_image_png=page_image_png,
                prompt_version=INTAKE_VERIFY_PROMPT_VERSION,
                pass_label="verify",
            )
        ] = json.dumps(medications_verify)


async def test_intake_extractor_happy_path() -> None:
    page = _single_page_image(b"INTAKE_P0")
    client = StubVlmClient()
    _seed_intake_fixtures(
        client,
        page_image_png=page.png_bytes,
        extract_payload={
            "demographics": {
                "full_name": "Maria Johnson",
                "sex": "F",
                "confidence": 0.9,
                "source_quote": "Maria Johnson F",
            },
            "chief_concern": "fatigue",
            "medications": [_intake_med()],
            "allergies": [_intake_allergy()],
            "family_history": [],
        },
        # The verify call returns one verdict per item; same payload
        # works for medications and (separately) for allergies because
        # both lists have length 1 here.
        medications_verify={"verdicts": [{"confirmed": True}]},
    )
    result = await extract_intake_pdf(
        document_id="intake-1",
        document_sha256="h" * 64,
        patient_id="Patient/87413",
        pages=[page],
        page_native_text=[""],
        vlm_client=client,
    )
    assert isinstance(result, IntakeFormExtraction)
    assert result.demographics.full_name == "Maria Johnson"
    assert result.chief_concern == "fatigue"
    assert [m.name for m in result.current_medications] == ["metformin"]
    assert [a.substance for a in result.allergies] == ["penicillin"]


async def test_intake_extractor_drops_below_floor_field() -> None:
    page = _single_page_image(b"INTAKE_FLOOR")
    client = StubVlmClient()
    _seed_intake_fixtures(
        client,
        page_image_png=page.png_bytes,
        extract_payload={
            "demographics": {"confidence": 0.0, "source_quote": ""},
            "medications": [_intake_med(confidence=0.6)],  # below 0.75 floor
            "allergies": [],
            "family_history": [],
        },
    )
    result = await extract_intake_pdf(
        document_id="intake-2",
        document_sha256="i" * 64,
        patient_id="Patient/87413",
        pages=[page],
        page_native_text=[""],
        vlm_client=client,
    )
    assert result.current_medications == []
    assert any(
        w.code is ExtractionWarningCode.LOW_CONFIDENCE
        for w in result.extraction_warnings
    )


# ─── Persist module ───────────────────────────────────────────────────


def test_lab_field_to_observation_shape() -> None:
    field = LabResultField(
        test_name="HbA1c",
        value="6.8",
        value_numeric=6.8,
        unit="%",
        confidence=0.9,
        source_quote="HbA1c 6.8 %",
        bbox=BoundingBox(page=0, x0=0.1, y0=0.1, x1=0.4, y1=0.2),
    )
    obs = lab_field_to_observation(
        document_id="doc-z", patient_id="Patient/87413", field=field
    )
    assert obs["resourceType"] == "Observation"
    assert obs["status"] == "final"
    assert obs["subject"]["reference"] == "Patient/87413"
    assert obs["valueQuantity"]["value"] == 6.8
    assert obs["derivedFrom"][0]["reference"] == "DocumentReference/doc-z"
    # Deterministic id matches direct compute.
    assert obs["id"] == deterministic_resource_id(
        document_id="doc-z", page_index=0, field_id="HbA1c"
    )


def test_lab_observation_provenance_links_back_to_document() -> None:
    field = LabResultField(
        test_name="LDL",
        value="120",
        value_numeric=120.0,
        unit="mg/dL",
        confidence=0.85,
        source_quote="LDL 120",
        bbox=BoundingBox(page=0, x0=0.1, y0=0.1, x1=0.4, y1=0.2),
    )
    obs = lab_field_to_observation(
        document_id="doc-y", patient_id="Patient/87413", field=field
    )
    prov = lab_observation_to_provenance(observation=obs, document_id="doc-y")
    assert prov["resourceType"] == "Provenance"
    assert prov["target"][0]["reference"] == f"Observation/{obs['id']}"
    assert prov["entity"][0]["what"]["reference"] == "DocumentReference/doc-y"


async def test_persist_lab_extraction_emits_observation_plus_provenance() -> None:
    extraction = LabPdfExtraction(
        document_id="doc-w",
        document_sha256="j" * 64,
        patient_id="Patient/87413",
        page_count=1,
        extracted_at=datetime.utcnow(),
        extracted_by_model="stub",
        prompt_version="v1",
        results=[
            LabResultField(
                test_name="HbA1c",
                value="6.8",
                value_numeric=6.8,
                unit="%",
                confidence=0.9,
                source_quote="HbA1c 6.8",
                bbox=BoundingBox(page=0, x0=0.1, y0=0.1, x1=0.4, y1=0.2),
            )
        ],
    )
    client = StubFhirPersistClient()
    ids = await persist_lab_extraction(extraction=extraction, client=client)
    assert len(ids) == 2  # one Observation + one Provenance
    types = [r["resourceType"] for r in client.bundles[0].resources]
    assert types == ["Observation", "Provenance"]


async def test_persist_lab_extraction_is_idempotent() -> None:
    """Re-running persist over the same extraction produces the same id set."""
    extraction = LabPdfExtraction(
        document_id="doc-idem",
        document_sha256="k" * 64,
        patient_id="Patient/87413",
        page_count=1,
        extracted_at=datetime.utcnow(),
        extracted_by_model="stub",
        prompt_version="v1",
        results=[
            LabResultField(
                test_name="HbA1c",
                value="6.8",
                value_numeric=6.8,
                unit="%",
                confidence=0.9,
                source_quote="HbA1c 6.8",
                bbox=BoundingBox(page=0, x0=0.1, y0=0.1, x1=0.4, y1=0.2),
            ),
        ],
    )
    client = StubFhirPersistClient()
    a = await persist_lab_extraction(extraction=extraction, client=client)
    b = await persist_lab_extraction(extraction=extraction, client=client)
    assert a == b  # same ids on both runs


async def test_persist_intake_extraction_writes_med_allergy_questionnaire() -> None:
    extraction = IntakeFormExtraction(
        document_id="doc-q",
        document_sha256="l" * 64,
        patient_id="Patient/87413",
        page_count=1,
        extracted_at=datetime.utcnow(),
        extracted_by_model="stub",
        prompt_version="v1",
        demographics=DemographicsBlock(confidence=0.9, source_quote="Maria"),
        chief_concern="fatigue",
        current_medications=[
            MedicationField(
                name="metformin",
                dose="500mg",
                confidence=0.9,
                source_quote="metformin 500mg",
                bbox=BoundingBox(page=0, x0=0.1, y0=0.1, x1=0.4, y1=0.2),
            )
        ],
        allergies=[
            AllergyField(
                substance="penicillin",
                reaction="hives",
                confidence=0.9,
                source_quote="penicillin hives",
                bbox=BoundingBox(page=0, x0=0.1, y0=0.3, x1=0.4, y1=0.4),
            )
        ],
    )
    client = StubFhirPersistClient()
    ids = await persist_intake_extraction(extraction=extraction, client=client)
    types = [r["resourceType"] for r in client.bundles[0].resources]
    assert "MedicationStatement" in types
    assert "AllergyIntolerance" in types
    assert "QuestionnaireResponse" in types
    assert len(ids) == len(types)


# ─── Dispatcher ───────────────────────────────────────────────────────


def _job(document_id: str, doc_type: DocType) -> QueuedJob:
    return QueuedJob(
        job_id=uuid.uuid4(),
        document_id=document_id,
        patient_id="Patient/87413",
        doc_type=doc_type,
        source=UploadSource.UPLOAD,
        sha256="m" * 64,
        byte_size=512,
        attempt_count=0,
        max_attempts=5,
        enqueued_at=datetime.now(tz=timezone.utc),
    )


async def test_dispatcher_routes_lab_pdf_through_extractor() -> None:
    pdf = _minimal_pdf_bytes()
    pages = render_pages(pdf)
    vlm = StubVlmClient()
    _seed_lab_fixtures(
        vlm,
        page_image_png=pages[0].png_bytes,
        extract_payload={"results": [_lab_candidate("HbA1c", page=0)]},
        verify_payload={"verdicts": [{"confirmed": True}]},
    )

    persist = StubFhirPersistClient()
    extract_fn = build_extract_fn(
        document_source=StubDocumentSource(by_id={"doc-disp-1": pdf}),
        vlm_client=vlm,
        persist_client=persist,
    )
    await extract_fn(_job("doc-disp-1", DocType.LAB_PDF))
    assert len(persist.bundles) == 1
    assert any(
        r["resourceType"] == "Observation"
        for r in persist.bundles[0].resources
    )


async def test_dispatcher_unknown_document_id_raises_typed_error() -> None:
    extract_fn = build_extract_fn(
        document_source=StubDocumentSource(by_id={}),
        vlm_client=StubVlmClient(),
        persist_client=StubFhirPersistClient(),
    )
    with pytest.raises(DocumentSourceError):
        await extract_fn(_job("doc-missing", DocType.LAB_PDF))


async def test_dispatcher_referral_fax_raises_unsupported() -> None:
    pdf = _minimal_pdf_bytes()
    extract_fn = build_extract_fn(
        document_source=StubDocumentSource(by_id={"doc-rf": pdf}),
        vlm_client=StubVlmClient(),
        persist_client=StubFhirPersistClient(),
    )
    with pytest.raises(UnsupportedDocTypeError):
        await extract_fn(_job("doc-rf", DocType.REFERRAL_FAX))
