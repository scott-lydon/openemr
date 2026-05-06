#!/usr/bin/env python3
"""Build a small clinical-guideline corpus and load it into pgvector.

The Week 2 RAG retriever needs a non-empty corpus or it returns
nothing useful. This loader ships a hand-curated set of ~25 guideline
snippets covering the rubric's scope (diabetes, hypertension, lipids,
gout, screening, immunization). Each snippet has a stable id, a
real-world anchor URL, a license URL, and a short body.

Embedding strategy:

- ``StubEmbedder`` (deterministic hash-derived 16-dim vector) by
  default. This is enough to retrieve a chunk by exact-match query
  semantics — the chunk for "ADA HbA1c target" comes back when the
  query mentions "HbA1c target" because both produce similar token
  hashes. Good for offline demos and CI.
- Set ``COPILOT_USE_OPENAI_EMBEDDINGS=true`` and provide
  ``OPENAI_API_KEY`` to switch to ``text-embedding-3-large`` truncated
  to 1024 dimensions. Strongly recommended for the demo recording —
  semantic recall is dramatically better.

Usage::

    [Local Mac terminal]
    cd clinical-copilot
    docker compose up --detach --wait postgres
    alembic upgrade head
    python -m sidecar.rag.build_corpus

Re-run safe — uses ``ON CONFLICT DO UPDATE`` so re-importing replaces
existing rows.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)


# ─── Curated snippets ────────────────────────────────────────────────


@dataclass(frozen=True)
class Snippet:
    chunk_id: str
    source_id: str
    section_path: str
    anchor_url: str
    license_url: str
    text: str
    domain_tags: tuple[str, ...]


CORPUS: list[Snippet] = [
    Snippet(
        chunk_id="ada-2025-glycemic-target-7pct",
        source_id="ADA-Standards-of-Care-2025",
        section_path="Standards of Care 2025 > 6. Glycemic Targets",
        anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
        license_url="https://diabetesjournals.org/care/about/permissions",
        text=(
            "For most non-pregnant adults with type 2 diabetes, the American "
            "Diabetes Association recommends an HbA1c goal of less than 7%. "
            "More stringent goals (such as <6.5%) may be appropriate if "
            "achievable without significant hypoglycemia. Less stringent "
            "goals (such as <8%) may be appropriate for patients with a "
            "history of severe hypoglycemia, limited life expectancy, or "
            "extensive comorbid conditions."
        ),
        domain_tags=("diabetes",),
    ),
    Snippet(
        chunk_id="ada-2025-metformin-first-line",
        source_id="ADA-Standards-of-Care-2025",
        section_path="Standards of Care 2025 > 9. Pharmacologic Approaches",
        anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
        license_url="https://diabetesjournals.org/care/about/permissions",
        text=(
            "Metformin is the preferred initial pharmacologic agent for "
            "type 2 diabetes unless contraindicated. It should be continued "
            "as long as it is tolerated and not contraindicated. Add other "
            "agents, including insulin, as needed to achieve glycemic goals."
        ),
        domain_tags=("diabetes",),
    ),
    Snippet(
        chunk_id="ada-2025-cv-risk-statin",
        source_id="ADA-Standards-of-Care-2025",
        section_path="Standards of Care 2025 > 10. Cardiovascular Disease and Risk Management",
        anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
        license_url="https://diabetesjournals.org/care/about/permissions",
        text=(
            "Adults aged 40-75 years with diabetes and additional ASCVD risk "
            "factors should receive moderate- to high-intensity statin therapy. "
            "For patients with established ASCVD, high-intensity statin therapy "
            "is recommended in addition to lifestyle modification."
        ),
        domain_tags=("diabetes", "lipids", "cardiovascular"),
    ),
    Snippet(
        chunk_id="ada-2025-bp-target-130-80",
        source_id="ADA-Standards-of-Care-2025",
        section_path="Standards of Care 2025 > 10. Cardiovascular Disease and Risk Management",
        anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
        license_url="https://diabetesjournals.org/care/about/permissions",
        text=(
            "For people with diabetes and hypertension, the blood pressure "
            "target is less than 130/80 mmHg, if it can be safely attained. "
            "Lifestyle intervention should be initiated for blood pressure "
            "above 120/80 mmHg."
        ),
        domain_tags=("diabetes", "hypertension"),
    ),
    Snippet(
        chunk_id="aha-2017-htn-stage-1",
        source_id="AHA-Hypertension-Guideline-2017",
        section_path="ACC/AHA Hypertension Guideline > Stage 1 Threshold",
        anchor_url="https://www.ahajournals.org/doi/10.1161/HYP.0000000000000065",
        license_url="https://www.ahajournals.org/permissions",
        text=(
            "Stage 1 hypertension is defined as a systolic blood pressure of "
            "130 to 139 mmHg or a diastolic of 80 to 89 mmHg. The 2017 "
            "ACC/AHA guideline recommends nonpharmacologic therapy for all "
            "patients with stage 1 hypertension and antihypertensive medication "
            "for those with established cardiovascular disease or 10-year ASCVD "
            "risk of 10% or higher."
        ),
        domain_tags=("hypertension", "cardiovascular"),
    ),
    Snippet(
        chunk_id="aha-2017-bp-measurement",
        source_id="AHA-Hypertension-Guideline-2017",
        section_path="ACC/AHA Hypertension Guideline > Accurate Measurement",
        anchor_url="https://www.ahajournals.org/doi/10.1161/HYP.0000000000000065",
        license_url="https://www.ahajournals.org/permissions",
        text=(
            "Out-of-office and self-monitored blood pressure measurements are "
            "recommended to confirm the diagnosis of hypertension and for "
            "titration of blood-pressure-lowering medication, in conjunction "
            "with telehealth or clinician guidance."
        ),
        domain_tags=("hypertension",),
    ),
    Snippet(
        chunk_id="acr-2020-gout-allopurinol-first",
        source_id="ACR-Gout-Guideline-2020",
        section_path="ACR Gout Guideline 2020 > Pharmacologic Urate-Lowering Therapy",
        anchor_url="https://www.rheumatology.org/Practice-Quality/Clinical-Support/Clinical-Practice-Guidelines/Gout",
        license_url="https://www.rheumatology.org/Permissions",
        text=(
            "Allopurinol is strongly recommended as the preferred first-line "
            "urate-lowering therapy for all patients with gout, including "
            "those with chronic kidney disease stage 3 and above. Initiation "
            "should start at 100 mg/day or less, with gradual titration to a "
            "target serum urate of less than 6 mg/dL."
        ),
        domain_tags=("gout",),
    ),
    Snippet(
        chunk_id="acr-2020-gout-acute-flare",
        source_id="ACR-Gout-Guideline-2020",
        section_path="ACR Gout Guideline 2020 > Acute Flare Management",
        anchor_url="https://www.rheumatology.org/Practice-Quality/Clinical-Support/Clinical-Practice-Guidelines/Gout",
        license_url="https://www.rheumatology.org/Permissions",
        text=(
            "For an acute gout flare, the ACR conditionally recommends "
            "colchicine, NSAIDs, or glucocorticoids (oral, intra-articular, "
            "or intramuscular) over IL-1 inhibitors as first-line therapy. "
            "Choice depends on patient comorbidities and contraindications."
        ),
        domain_tags=("gout",),
    ),
    Snippet(
        chunk_id="acr-2020-gout-diagnostic-aspirate",
        source_id="ACR-Gout-Guideline-2020",
        section_path="ACR Gout Guideline 2020 > Diagnosis",
        anchor_url="https://www.rheumatology.org/Practice-Quality/Clinical-Support/Clinical-Practice-Guidelines/Gout",
        license_url="https://www.rheumatology.org/Permissions",
        text=(
            "Definitive diagnosis of gout requires identification of "
            "monosodium urate crystals in synovial fluid or a tophus "
            "aspirate. Where joint aspiration is not feasible, ultrasound "
            "or dual-energy CT may support the diagnosis. Serum uric acid "
            "alone is neither sensitive nor specific for an acute flare."
        ),
        domain_tags=("gout",),
    ),
    Snippet(
        chunk_id="uspstf-screen-prediabetes-2021",
        source_id="USPSTF-Prediabetes-Screening-2021",
        section_path="USPSTF Recommendation > Prediabetes and Type 2 Diabetes Screening",
        anchor_url="https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/screening-for-prediabetes-and-type-2-diabetes",
        license_url="https://www.uspreventiveservicestaskforce.org/uspstf/about-uspstf/methods-and-processes",
        text=(
            "The USPSTF recommends screening for prediabetes and type 2 "
            "diabetes in adults aged 35 to 70 years who have overweight or "
            "obesity. Clinicians should offer or refer patients with "
            "prediabetes to effective preventive interventions. (Grade B)"
        ),
        domain_tags=("screening", "diabetes"),
    ),
    Snippet(
        chunk_id="uspstf-screen-colorectal-2021",
        source_id="USPSTF-Colorectal-Cancer-Screening-2021",
        section_path="USPSTF Recommendation > Colorectal Cancer Screening",
        anchor_url="https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/colorectal-cancer-screening",
        license_url="https://www.uspreventiveservicestaskforce.org/uspstf/about-uspstf/methods-and-processes",
        text=(
            "The USPSTF recommends screening for colorectal cancer in all "
            "adults aged 45 to 75 years (Grade B for ages 45-49, Grade A for "
            "ages 50-75). For adults aged 76 to 85, the decision should be "
            "individualized based on overall health, prior screening history, "
            "and patient preferences."
        ),
        domain_tags=("screening", "oncology"),
    ),
    Snippet(
        chunk_id="uspstf-screen-depression-2023",
        source_id="USPSTF-Depression-Screening-2023",
        section_path="USPSTF Recommendation > Depression Screening (Adults)",
        anchor_url="https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/screening-depression-suicide-risk-adults",
        license_url="https://www.uspreventiveservicestaskforce.org/uspstf/about-uspstf/methods-and-processes",
        text=(
            "The USPSTF recommends screening for depression in the general "
            "adult population, including pregnant and postpartum persons. "
            "Screening should be implemented with adequate systems in place "
            "to ensure accurate diagnosis, effective treatment, and appropriate "
            "follow-up. (Grade B) The PHQ-9 is a commonly used screening tool."
        ),
        domain_tags=("screening", "mental_health"),
    ),
    Snippet(
        chunk_id="uspstf-screen-statin-2022",
        source_id="USPSTF-Statin-Use-Primary-Prevention-2022",
        section_path="USPSTF Recommendation > Statin Use for Primary Prevention",
        anchor_url="https://www.uspreventiveservicestaskforce.org/uspstf/recommendation/statin-use-in-adults-preventive-medication",
        license_url="https://www.uspreventiveservicestaskforce.org/uspstf/about-uspstf/methods-and-processes",
        text=(
            "The USPSTF recommends initiating statin use for primary prevention "
            "of cardiovascular disease in adults aged 40 to 75 years who have "
            "one or more cardiovascular risk factors and an estimated 10-year "
            "ASCVD risk of 10% or higher. (Grade B)"
        ),
        domain_tags=("screening", "lipids", "cardiovascular"),
    ),
    Snippet(
        chunk_id="cdc-immunization-shingles-2026",
        source_id="CDC-Adult-Immunization-Schedule-2026",
        section_path="CDC Adult Immunization Schedule > Recombinant Zoster Vaccine",
        anchor_url="https://www.cdc.gov/vaccines/schedules/hcp/imz/adult.html",
        license_url="https://www.cdc.gov/about/policies/copyright.html",
        text=(
            "Adults aged 50 years and older should receive two doses of "
            "recombinant zoster vaccine (Shingrix) separated by 2 to 6 months "
            "to prevent shingles and post-herpetic neuralgia. Vaccination is "
            "also recommended for immunocompromised adults aged 19 and older."
        ),
        domain_tags=("immunization",),
    ),
    Snippet(
        chunk_id="cdc-immunization-pneumococcal-2026",
        source_id="CDC-Adult-Immunization-Schedule-2026",
        section_path="CDC Adult Immunization Schedule > Pneumococcal Vaccine",
        anchor_url="https://www.cdc.gov/vaccines/schedules/hcp/imz/adult.html",
        license_url="https://www.cdc.gov/about/policies/copyright.html",
        text=(
            "Adults aged 65 years and older without prior pneumococcal "
            "vaccination should receive PCV15, PCV20, or PCV21 followed by "
            "PPSV23 if PCV15 was used. Adults aged 19 to 64 with chronic "
            "medical conditions or immunocompromising conditions should also "
            "be vaccinated."
        ),
        domain_tags=("immunization",),
    ),
    Snippet(
        chunk_id="nof-2022-dxa-screening",
        source_id="National-Osteoporosis-Foundation-2022",
        section_path="NOF Clinician's Guide > Screening for Osteoporosis",
        anchor_url="https://www.bonehealthandosteoporosis.org/wp-content/uploads/Clinicians-Guide-to-Prevention-and-Treatment-of-Osteoporosis.pdf",
        license_url="https://www.bonehealthandosteoporosis.org/permissions",
        text=(
            "DXA bone-mineral-density measurement is recommended for all "
            "women aged 65 and older and for postmenopausal women under 65 "
            "with clinical risk factors for fracture. Men aged 70 and older "
            "should also be screened, with earlier screening for those with "
            "risk factors."
        ),
        domain_tags=("osteoporosis", "screening"),
    ),
    Snippet(
        chunk_id="ada-2025-ckd-egfr-monitoring",
        source_id="ADA-Standards-of-Care-2025",
        section_path="Standards of Care 2025 > 11. Chronic Kidney Disease",
        anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
        license_url="https://diabetesjournals.org/care/about/permissions",
        text=(
            "Patients with diabetes should have annual screening for "
            "albuminuria (urine albumin-to-creatinine ratio) and estimated "
            "glomerular filtration rate (eGFR) starting at diagnosis for "
            "type 2 diabetes and at 5 years post-diagnosis for type 1. "
            "More frequent monitoring is required as eGFR declines."
        ),
        domain_tags=("diabetes",),
    ),
    Snippet(
        chunk_id="aafp-2024-allergy-amoxicillin",
        source_id="AAFP-Drug-Allergy-2024",
        section_path="AAFP Practice Guidelines > Penicillin Allergy Evaluation",
        anchor_url="https://www.aafp.org/family-physician/practice-and-career/practice-resources/guidelines.html",
        license_url="https://www.aafp.org/copyright.html",
        text=(
            "A documented penicillin allergy precludes the safe use of "
            "amoxicillin and other beta-lactams. Cross-reactivity within the "
            "penicillin class is well-established; cross-reactivity with "
            "cephalosporins is approximately 1-2%. Allergy verification via "
            "skin testing or graded oral challenge is recommended for "
            "patients with vague or remote histories."
        ),
        domain_tags=(),
    ),
    Snippet(
        chunk_id="ada-2025-foot-exam",
        source_id="ADA-Standards-of-Care-2025",
        section_path="Standards of Care 2025 > 12. Microvascular Complications and Foot Care",
        anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
        license_url="https://diabetesjournals.org/care/about/permissions",
        text=(
            "Adults with diabetes should have a comprehensive foot evaluation "
            "at least annually to identify risk factors for ulcers and "
            "amputation. Patients with peripheral neuropathy or evidence of "
            "structural foot deformity should have their feet examined at "
            "every visit."
        ),
        domain_tags=("diabetes",),
    ),
    Snippet(
        chunk_id="ada-2025-eye-exam",
        source_id="ADA-Standards-of-Care-2025",
        section_path="Standards of Care 2025 > 12. Microvascular Complications",
        anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
        license_url="https://diabetesjournals.org/care/about/permissions",
        text=(
            "Patients with type 2 diabetes should have an initial dilated and "
            "comprehensive eye examination by an ophthalmologist or "
            "optometrist at the time of diabetes diagnosis. Subsequent "
            "examinations should occur at least every 1 to 2 years thereafter."
        ),
        domain_tags=("diabetes",),
    ),
    Snippet(
        chunk_id="aha-2018-lipids-ldl",
        source_id="AHA-Cholesterol-Guideline-2018",
        section_path="ACC/AHA Cholesterol Guideline > LDL Targets",
        anchor_url="https://www.ahajournals.org/doi/10.1161/CIR.0000000000000625",
        license_url="https://www.ahajournals.org/permissions",
        text=(
            "For very high-risk ASCVD patients, an LDL cholesterol threshold "
            "of 70 mg/dL guides the addition of nonstatin therapies. For "
            "primary prevention in adults 40-75 with diabetes, moderate-"
            "intensity statin therapy is recommended regardless of baseline "
            "LDL."
        ),
        domain_tags=("lipids", "cardiovascular"),
    ),
    Snippet(
        chunk_id="cw-2018-routine-creatinine",
        source_id="Choosing-Wisely-2018",
        section_path="Choosing Wisely > Routine Creatinine Testing",
        anchor_url="https://www.choosingwisely.org/clinician-lists/",
        license_url="https://www.choosingwisely.org/about-us/permissions/",
        text=(
            "Choosing Wisely advises against routine annual basic metabolic "
            "panel screening in low-risk asymptomatic adults. For patients "
            "with diabetes or hypertension, annual creatinine measurement is "
            "appropriate as it is part of standard chronic-disease monitoring."
        ),
        domain_tags=(),
    ),
    Snippet(
        chunk_id="ada-2025-aspirin-prevention",
        source_id="ADA-Standards-of-Care-2025",
        section_path="Standards of Care 2025 > 10. Cardiovascular Disease",
        anchor_url="https://diabetesjournals.org/care/issue/48/Supplement_1",
        license_url="https://diabetesjournals.org/care/about/permissions",
        text=(
            "Aspirin therapy (75-162 mg/day) for primary prevention may be "
            "considered for adults with diabetes who are at increased "
            "cardiovascular risk after a discussion of bleeding risk. Routine "
            "aspirin for primary prevention in adults at low cardiovascular "
            "risk is no longer recommended."
        ),
        domain_tags=("diabetes", "cardiovascular"),
    ),
]


# ─── Loader ──────────────────────────────────────────────────────────


def _embed_text(text: str, *, model: str, dimension: int) -> list[float]:
    """Return an embedding for ``text``.

    Uses OpenAI when ``COPILOT_USE_OPENAI_EMBEDDINGS=true`` and a key is
    available; otherwise falls back to the deterministic stub embedder
    so the loader works offline.
    """
    use_openai = os.environ.get("COPILOT_USE_OPENAI_EMBEDDINGS", "").lower() == "true"
    if use_openai and os.environ.get("OPENAI_API_KEY"):
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
            client = OpenAI()
            resp = client.embeddings.create(
                model="text-embedding-3-large",
                input=text,
                dimensions=dimension,
            )
            return list(resp.data[0].embedding)
        except Exception as exc:
            logger.warning(
                "OpenAI embed failed (%s); falling back to stub embedder.", exc,
            )
    from sidecar.rag.embeddings import embed_text_deterministic
    return embed_text_deterministic(text, dimension=dimension)


def upsert_snippets(snippets: Iterable[Snippet], *, dimension: int = 1024) -> int:
    """Insert / update each snippet into the ``guideline_chunks`` table.

    Returns the number of rows touched. The table must exist (run
    ``alembic upgrade head`` first).
    """
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is not installed. Install with `pip install -e .[postgres]`."
        ) from exc

    url = (
        os.environ.get("COPILOT_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://copilot:copilot@localhost:5433/copilot"
    )
    # psycopg accepts the bare 'postgresql://' form.
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql://", 1)

    use_openai = os.environ.get("COPILOT_USE_OPENAI_EMBEDDINGS", "").lower() == "true"
    embed_model = "text-embedding-3-large@1024" if use_openai else "stub-embedder-v1"

    # Pre-embed once so a Postgres outage during inserts doesn't cost
    # the embedding pass.
    rows: list[tuple[object, ...]] = []
    for snippet in snippets:
        vector = _embed_text(snippet.text, model=embed_model, dimension=dimension)
        if len(vector) != dimension:
            raise ValueError(
                f"embedding for {snippet.chunk_id!r} returned {len(vector)} "
                f"dims, expected {dimension}."
            )
        vec_lit = "[" + ",".join(f"{v:.6f}" for v in vector) + "]"
        rows.append((
            snippet.chunk_id,
            snippet.source_id,
            snippet.section_path,
            snippet.anchor_url,
            list(snippet.domain_tags),
            snippet.text,
            vec_lit,
            embed_model,
            snippet.license_url,
        ))

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO guideline_chunks (
                        chunk_id, source_id, section_path, anchor_url,
                        domain_tags, text, embedding, embedding_model,
                        license_url
                    ) VALUES (
                        %s, %s, %s, %s, %s::text[], %s, %s::vector, %s, %s
                    )
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        source_id = EXCLUDED.source_id,
                        section_path = EXCLUDED.section_path,
                        anchor_url = EXCLUDED.anchor_url,
                        domain_tags = EXCLUDED.domain_tags,
                        text = EXCLUDED.text,
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        license_url = EXCLUDED.license_url,
                        indexed_at = NOW();
                    """,
                    r,
                )
        conn.commit()

    return len(rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    use_openai = os.environ.get("COPILOT_USE_OPENAI_EMBEDDINGS", "").lower() == "true"
    if use_openai and not os.environ.get("OPENAI_API_KEY"):
        sys.stderr.write(
            "WARN: COPILOT_USE_OPENAI_EMBEDDINGS=true but OPENAI_API_KEY is not "
            "set. Falling back to the deterministic stub embedder.\n"
        )
    n = upsert_snippets(CORPUS, dimension=1024)
    print(f"Loaded {n} snippets into guideline_chunks "
          f"(embedder={'openai' if use_openai else 'stub'}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
