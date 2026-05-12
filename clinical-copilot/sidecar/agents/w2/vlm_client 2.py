"""Vision Language Model (VLM) client protocol and stub implementation.

The extractor calls ``VlmClient.extract_lab_page`` /
``extract_intake_page`` once per page; the protocol hides the concrete
provider (OpenAI, Anthropic, Azure OpenAI, mock) from the rest of the
pipeline.

Why a Protocol rather than an abstract base class:

- The mock implementation is a plain dataclass with no inheritance.
  Tests construct it with deterministic fixtures and inject it into the
  extractor without touching real network.
- Adding a new provider is a one-file change: implement the protocol,
  register it in ``vlm_factory.from_settings``.
- The protocol's surface is intentionally thin (one method per document
  type) so a provider implementation cannot accidentally leak unrelated
  state between pages.

The stub implementation in this module is the deterministic fixture
the unit tests use. It returns whatever was preloaded into
``StubVlmClient.fixtures`` keyed on a SHA-256 of the page image bytes
plus the prompt version. A test that wants to verify the
two-pass disagreement code wires the same key to a different value for
the verify pass.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class VlmExtractionRequest:
    """One page's worth of input for the extractor.

    ``page_image_png`` is the rasterized page; ``page_native_text`` is
    the selectable text from the PDF (empty string for scanned pages).
    Both halves go to the model.
    """

    document_id: str
    patient_id: str
    page_index: int
    page_image_png: bytes
    page_native_text: str
    prompt_version: str
    pass_label: str  # "extract" or "verify"


@dataclass(frozen=True)
class VlmExtractionResponse:
    """Raw structured-output JSON the model returned, plus provenance.

    Parsing this into a Pydantic schema is the extractor's job, not the
    client's, so a malformed response from the model surfaces as a
    Pydantic ValidationError the extractor catches.
    """

    response_json: str
    model_id: str
    completed_at: datetime
    input_tokens: int
    output_tokens: int


class VlmClient(Protocol):
    """The protocol every Vision Language Model implementation honors.

    Two methods, one per document type. The method signatures are
    independent so a future provider with different prompt-engineering
    quirks can specialize without leaking those quirks across types.
    """

    async def extract_lab_page(
        self, request: VlmExtractionRequest
    ) -> VlmExtractionResponse:
        ...

    async def extract_intake_page(
        self, request: VlmExtractionRequest
    ) -> VlmExtractionResponse:
        ...


def fixture_key(*, page_image_png: bytes, prompt_version: str, pass_label: str) -> str:
    """Stable lookup key for a stub fixture.

    The key includes the prompt version and the pass label so a single
    test can wire ``("..._extract", "..._verify")`` pairs that disagree.
    """
    digest = hashlib.sha256(page_image_png + prompt_version.encode() + pass_label.encode())
    return digest.hexdigest()


@dataclass
class StubVlmClient:
    """Deterministic VLM client used by unit tests.

    Construction:

        client = StubVlmClient()
        client.fixtures[fixture_key(...)] = '{"results": [...]}'
        client.fixtures[fixture_key(...)] = '{"results": []}'

    A request whose key is missing from ``fixtures`` raises
    ``KeyError`` so a forgotten fixture is loud, not silently returning
    an empty result.
    """

    fixtures: dict[str, str] = field(default_factory=dict)
    model_id: str = "stub-vlm"
    invocations: list[VlmExtractionRequest] = field(default_factory=list)

    async def extract_lab_page(
        self, request: VlmExtractionRequest
    ) -> VlmExtractionResponse:
        return self._respond(request)

    async def extract_intake_page(
        self, request: VlmExtractionRequest
    ) -> VlmExtractionResponse:
        return self._respond(request)

    def _respond(self, request: VlmExtractionRequest) -> VlmExtractionResponse:
        self.invocations.append(request)
        key = fixture_key(
            page_image_png=request.page_image_png,
            prompt_version=request.prompt_version,
            pass_label=request.pass_label,
        )
        if key not in self.fixtures:
            raise KeyError(
                f"StubVlmClient: no fixture registered for "
                f"page_index={request.page_index}, pass={request.pass_label}, "
                f"prompt={request.prompt_version}. Build the key with "
                f"vlm_client.fixture_key(...) and assign to .fixtures."
            )
        return VlmExtractionResponse(
            response_json=self.fixtures[key],
            model_id=self.model_id,
            completed_at=datetime.utcnow(),
            input_tokens=0,
            output_tokens=0,
        )


def parse_response_json(response: VlmExtractionResponse) -> object:
    """Parse the raw response JSON, surfacing decode errors with context.

    The extractor calls this before handing off to Pydantic. Surfacing
    JSON decode errors at this seam (rather than inside Pydantic's
    parser) gives clearer stack traces.
    """
    try:
        return json.loads(response.response_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"VLM response was not valid JSON. model={response.model_id} "
            f"error={exc!s} preview={response.response_json[:200]!r}"
        ) from exc


__all__ = [
    "StubVlmClient",
    "VlmClient",
    "VlmExtractionRequest",
    "VlmExtractionResponse",
    "fixture_key",
    "parse_response_json",
]
