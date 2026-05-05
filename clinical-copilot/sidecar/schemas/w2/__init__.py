"""Week 2 strict-output schemas.

Every model uses ``model_config = ConfigDict(extra="forbid")`` so the
Vision Language Model cannot smuggle fields the schema does not name.
A field with no bounding box must come with a non-empty ``source_quote``
so the citation contract has at least one anchor.

Public surface re-exported here so callers can ``from sidecar.schemas.w2
import LabPdfExtraction`` without reaching into module paths.
"""

from sidecar.schemas.w2.bbox import BoundingBox
from sidecar.schemas.w2.citation import Citation, CitationSourceType
from sidecar.schemas.w2.extraction_warning import ExtractionWarning, ExtractionWarningCode
from sidecar.schemas.w2.intake import (
    AllergyField,
    DemographicsBlock,
    FamilyHistoryField,
    IntakeFormExtraction,
    MedicationField,
    SexCode,
)
from sidecar.schemas.w2.lab import AbnormalFlag, LabPdfExtraction, LabResultField

__all__ = [
    "AbnormalFlag",
    "AllergyField",
    "BoundingBox",
    "Citation",
    "CitationSourceType",
    "DemographicsBlock",
    "ExtractionWarning",
    "ExtractionWarningCode",
    "FamilyHistoryField",
    "IntakeFormExtraction",
    "LabPdfExtraction",
    "LabResultField",
    "MedicationField",
    "SexCode",
]
