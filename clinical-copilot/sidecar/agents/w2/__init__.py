"""Week 2 (Multimodal Evidence Agent) extractor package.

Public surface re-exported here so callers can import from a single
namespace::

    from sidecar.agents.w2 import build_extract_fn, StubDocumentSource

Modules:

- ``vlm_client`` — Vision Language Model protocol + stub.
- ``lab_extractor`` — two-pass lab PDF extractor.
- ``intake_extractor`` — two-pass intake form extractor.
- ``extract_dispatcher`` — binds the worker's ExtractFn protocol to
  render → extract → persist.
"""

from sidecar.agents.w2.evidence_packet_builder import build_evidence_packet
from sidecar.agents.w2.extract_dispatcher import (
    DocumentSource,
    DocumentSourceError,
    StubDocumentSource,
    UnsupportedDocTypeError,
    build_extract_fn,
)
from sidecar.agents.w2.graph import (
    EvidenceRetrieverNode,
    GraphNodes,
    GraphResult,
    IntakeExtractorNode,
    PairwiseComparerNode,
    run_graph,
)
from sidecar.agents.w2.response_formatter import format_response
from sidecar.agents.w2.state import (
    ClinicalClaim,
    DecisionPath,
    GraphState,
    IntentKind,
    ResponsePacket,
    WorkerName,
)
from sidecar.agents.w2.supervisor import (
    SUPERVISOR_JUDGE_PROMPT_VERSION,
    StubSupervisorJudge,
    SupervisorDecision,
    SupervisorJudge,
    preflight,
    supervise,
)
from sidecar.agents.w2.verifier import (
    CitationResolver,
    PresidioUnavailable,
    StubCitationResolver,
    VerifierConfig,
    verify,
)
from sidecar.agents.w2.intake_extractor import (
    INTAKE_EXTRACT_PROMPT_VERSION,
    INTAKE_VERIFY_PROMPT_VERSION,
    IntakeExtractionFailed,
    extract_intake_pdf,
)
from sidecar.agents.w2.lab_extractor import (
    LAB_EXTRACT_PROMPT_VERSION,
    LAB_VERIFY_PROMPT_VERSION,
    LabExtractionFailed,
    extract_lab_pdf,
)
from sidecar.agents.w2.vlm_client import (
    StubVlmClient,
    VlmClient,
    VlmExtractionRequest,
    VlmExtractionResponse,
    fixture_key,
    parse_response_json,
)

__all__ = [
    "CitationResolver",
    "ClinicalClaim",
    "DecisionPath",
    "DocumentSource",
    "DocumentSourceError",
    "EvidenceRetrieverNode",
    "GraphNodes",
    "GraphResult",
    "GraphState",
    "IntakeExtractorNode",
    "IntentKind",
    "PairwiseComparerNode",
    "PresidioUnavailable",
    "ResponsePacket",
    "SUPERVISOR_JUDGE_PROMPT_VERSION",
    "StubCitationResolver",
    "StubSupervisorJudge",
    "SupervisorDecision",
    "SupervisorJudge",
    "VerifierConfig",
    "WorkerName",
    "build_evidence_packet",
    "format_response",
    "preflight",
    "run_graph",
    "supervise",
    "verify",
    "INTAKE_EXTRACT_PROMPT_VERSION",
    "INTAKE_VERIFY_PROMPT_VERSION",
    "IntakeExtractionFailed",
    "LAB_EXTRACT_PROMPT_VERSION",
    "LAB_VERIFY_PROMPT_VERSION",
    "LabExtractionFailed",
    "StubDocumentSource",
    "StubVlmClient",
    "UnsupportedDocTypeError",
    "VlmClient",
    "VlmExtractionRequest",
    "VlmExtractionResponse",
    "build_extract_fn",
    "extract_intake_pdf",
    "extract_lab_pdf",
    "fixture_key",
    "parse_response_json",
]
