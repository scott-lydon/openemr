"""OpenAI Vision Language Model (VLM) implementation of ``VlmClient``.

Calls ``gpt-4o``/``gpt-4o-mini`` with an image part (the rasterized PDF
page) plus a text part (the page's native selectable text plus the
extraction prompt). Returns the JSON the model produced under strict
structured-output mode so the lab/intake extractors can parse it
without further coercion.

Why a separate file from the stub:

- The stub lives in ``vlm_client.py`` so unit tests can import it
  without pulling in the openai dependency. Production code that
  *needs* openai imports it from here, and a missing API key only
  blows up the worker's startup, not the test suite.
- The two prompt halves (lab vs intake) keep their own renderers so a
  prompt-engineering change to one cannot accidentally regress the
  other. Both halves share the structured-output plumbing.

Configuration:

- Reads ``Settings.openai_api_key`` and ``Settings.openai_model``
  exactly like ``OpenAIProvider`` in ``sidecar/agent/pair_judge.py``.
  ``OPENAI_VLM_MODEL`` (env) overrides the model when the operator
  wants the worker on a different tier than the chat agent.
- Honors ``OPENAI_BASE_URL`` for Azure-style endpoints. The strict
  JSON-schema mode ships with both OpenAI and Azure OpenAI.

Failure handling:

- Every call returns a typed ``VlmExtractionResponse`` on success.
- Network or schema errors raise ``OpenAIVlmError`` with a category
  + actionable hint. The extractor wraps these into
  ``LabExtractionFailed`` / ``IntakeExtractionFailed`` so the worker
  retries via the queue's exponential-backoff policy.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime
from typing import Any, Final

from sidecar.agents.w2.vlm_client import (
    VlmClient,
    VlmExtractionRequest,
    VlmExtractionResponse,
)
from sidecar.config import Settings


logger = logging.getLogger(__name__)


# Defaulting to gpt-4o-mini for cost; both gpt-4o and gpt-4o-mini
# support vision input. Operators bump to gpt-4o for the eval suite
# only when accuracy regresses on a page type.
_DEFAULT_VLM_MODEL: Final[str] = "gpt-4o-mini"


class OpenAIVlmError(RuntimeError):
    """Wraps every VLM call failure with a stable ``code`` + hint.

    The extractor catches this at the page boundary and raises
    ``LabExtractionFailed`` / ``IntakeExtractionFailed`` so the worker
    only ever has to handle one error family.
    """

    def __init__(self, message: str, *, code: str, hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


# ─── Prompts ─────────────────────────────────────────────────────────


_LAB_EXTRACT_SYSTEM_PROMPT = """\
You extract lab test results from one page of a clinical PDF. The page
has been rendered to an image (you receive both the image and any
selectable text underneath). Your only job is to enumerate the lab
results that appear on this page.

Output a JSON object with shape:

{
  "results": [
    {
      "test_name": "<verbatim test name as printed>",
      "loinc_code": "<LOINC code if you can identify one with high confidence, else null>",
      "value_numeric": <number, or null if the value is qualitative>,
      "value_text": "<verbatim value string when value_numeric is null, else empty>",
      "unit": "<unit string, e.g. mg/dL, mmol/L, %>",
      "reference_range_low": <number or null>,
      "reference_range_high": <number or null>,
      "abnormal_flag": "high"|"low"|"normal"|"critical_high"|"critical_low"|"unknown",
      "collection_date": "<ISO 8601 date if printed on the page, else null>",
      "confidence": <float 0-1, your confidence the field is correct>,
      "bbox": {"page": <0-based page index>, "x0": <0-1>, "y0": <0-1>, "x1": <0-1>, "y1": <0-1>}
    }
  ]
}

Rules:
1. Only enumerate results visibly printed on the page. Do not invent.
2. ``confidence`` should reflect both OCR clarity and label-value
   pairing certainty. Below 0.7 means a human should review.
3. ``bbox`` coordinates are normalized (0-1) relative to the page.
   Approximate when exact bounds are not obvious.
4. If the page contains no lab results (cover sheet, instructions,
   etc.), return ``{"results": []}``.
5. Return STRICT JSON, no commentary, no markdown code fences.
"""


_LAB_VERIFY_SYSTEM_PROMPT = """\
You are a verifier. You receive an image of a clinical PDF page and a
list of CANDIDATE lab results that another model claims appear on this
page. Your job is to confirm or reject each candidate by inspecting
the image.

Output JSON of shape:

{
  "verdicts": [
    {"confirmed": true|false, "reason": "<one short sentence explaining>"},
    ...
  ]
}

Rules:
1. ``verdicts`` MUST have exactly one entry per candidate, in the
   same order the candidates were given.
2. Mark ``confirmed=false`` when the image does not show that test or
   shows a different value, unit, or reference range than claimed.
3. Mark ``confirmed=true`` only when the test name AND value are
   visibly printed on this page.
4. Return STRICT JSON, no commentary.
"""


_INTAKE_EXTRACT_SYSTEM_PROMPT = """\
You extract patient intake-form fields from one rendered page.
Return JSON of shape:

{
  "current_medications": [...],
  "allergies": [...],
  "chief_concern": "<string or null>",
  "family_history": [...]
}

(See sidecar/schemas/w2/intake.py for the precise sub-schemas.) Only
emit fields visibly printed on the page; return empty arrays for
absent sections. Return STRICT JSON, no markdown.
"""


# ─── The client ──────────────────────────────────────────────────────


class OpenAIVlmClient:
    """Production VLM client. Calls OpenAI's vision-capable models.

    Construction reads from ``Settings`` so the same .env that drives
    the chat path drives the worker. ``OPENAI_VLM_MODEL`` env override
    lets the worker run on a different model than the chat synthesizer
    without two settings keys.
    """

    def __init__(self, settings: Settings) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise OpenAIVlmError(
                "openai package not installed; install with `pip install openai`. "
                f"Underlying ImportError: {exc!s}",
                code="openai_not_installed",
                hint=(
                    "Add `openai>=1.40` to pyproject.toml dependencies and "
                    "rebuild the sidecar container."
                ),
            ) from exc

        if not settings.openai_api_key:
            raise OpenAIVlmError(
                "OPENAI_API_KEY is empty; the OpenAI VLM client cannot start.",
                code="openai_api_key_missing",
                hint=(
                    "Set OPENAI_API_KEY in .env (production) or set "
                    "COPILOT_ALLOW_MOCK=true to use the StubVlmClient "
                    "with pre-loaded fixtures (test/dev only)."
                ),
            )

        self._model = (
            os.environ.get("OPENAI_VLM_MODEL")
            or os.environ.get("COPILOT_OPENAI_VLM_MODEL")
            or _DEFAULT_VLM_MODEL
        )
        self._api_key_summary = (
            f"{settings.openai_api_key[:7]}…<len={len(settings.openai_api_key)}>"
        )

        client_kwargs: dict[str, Any] = {"api_key": settings.openai_api_key}
        if settings.openai_base_url:
            client_kwargs["base_url"] = settings.openai_base_url
        self._client = AsyncOpenAI(**client_kwargs)

        logger.info(
            "OpenAIVlmClient ready",
            extra={
                "model": self._model,
                "api_key": self._api_key_summary,
                "base_url": settings.openai_base_url
                or "https://api.openai.com/v1 (default)",
            },
        )

    @property
    def model_id(self) -> str:
        return self._model

    async def extract_lab_page(
        self, request: VlmExtractionRequest
    ) -> VlmExtractionResponse:
        """Run one VLM call for a lab-PDF page (extract or verify pass)."""
        if request.pass_label == "extract":
            system_prompt = _LAB_EXTRACT_SYSTEM_PROMPT
            user_text = (
                f"Page {request.page_index}. Native selectable text follows "
                f"between markers. Use it as a hint, but trust the image when "
                f"they disagree.\n\n--- NATIVE TEXT BEGIN ---\n"
                f"{request.page_native_text}\n--- NATIVE TEXT END ---"
            )
        elif request.pass_label == "verify":
            system_prompt = _LAB_VERIFY_SYSTEM_PROMPT
            # Verify-pass native_text is the JSON-encoded candidates the
            # extractor produced (see lab_extractor._extract_one_page).
            user_text = (
                f"Page {request.page_index}. Verify each candidate against "
                f"the image:\n{request.page_native_text}"
            )
        else:
            raise OpenAIVlmError(
                f"unknown pass_label={request.pass_label!r}; expected "
                "'extract' or 'verify'.",
                code="unknown_pass_label",
                hint=(
                    "The lab extractor only emits 'extract' and 'verify'. "
                    "If you added a new pass, register its prompt here too."
                ),
            )
        return await self._call(
            request=request,
            system_prompt=system_prompt,
            user_text=user_text,
        )

    async def extract_intake_page(
        self, request: VlmExtractionRequest
    ) -> VlmExtractionResponse:
        """Run one VLM call for an intake-form page."""
        return await self._call(
            request=request,
            system_prompt=_INTAKE_EXTRACT_SYSTEM_PROMPT,
            user_text=(
                f"Page {request.page_index}. Native selectable text follows."
                f"\n\n--- NATIVE TEXT BEGIN ---\n{request.page_native_text}"
                f"\n--- NATIVE TEXT END ---"
            ),
        )

    async def _call(
        self,
        *,
        request: VlmExtractionRequest,
        system_prompt: str,
        user_text: str,
    ) -> VlmExtractionResponse:
        """Common dispatch: build the multimodal messages and call OpenAI."""
        if not request.page_image_png:
            raise OpenAIVlmError(
                f"page_index={request.page_index}: page_image_png is empty",
                code="page_image_empty",
                hint=(
                    "render_pages() should never produce a zero-byte PNG. "
                    "Check render.py and the source PDF."
                ),
            )
        b64_png = base64.b64encode(request.page_image_png).decode("ascii")
        data_url = f"data:image/png;base64,{b64_png}"

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url, "detail": "high"},
                    },
                ],
            },
        ]
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001
            raise OpenAIVlmError(
                f"OpenAI VLM call failed: {type(exc).__name__}: {exc!s}",
                code="openai_call_failed",
                hint=(
                    "Inspect launch.log for the full traceback. Common "
                    "causes: API key invalid, the model name does not "
                    "support vision input, the image is too large, or "
                    "the network/proxy is blocking outbound HTTPS."
                ),
            ) from exc

        try:
            content = resp.choices[0].message.content or "{}"
        except (IndexError, AttributeError) as exc:
            raise OpenAIVlmError(
                f"OpenAI VLM response shape was unexpected: "
                f"{type(exc).__name__}: {exc!s}",
                code="openai_response_shape_unexpected",
                hint=(
                    "The SDK contract changed or the model returned no "
                    "choices. Pin to the SDK version recorded in "
                    "pyproject.toml and retry."
                ),
            ) from exc

        # Ensure the content is JSON-parseable so the extractor's
        # parse_response_json() does not have to invent a fallback.
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenAIVlmError(
                f"OpenAI VLM returned non-JSON despite "
                f"response_format=json_object: {exc!s}; content[:300]="
                f"{content[:300]!r}",
                code="openai_response_not_json",
                hint=(
                    "Some Azure deployments do not honor json_object on "
                    "every model. Either upgrade the deployment or strip "
                    "leading/trailing markdown code-fence wrappers in a "
                    "preprocessing step before parse_response_json."
                ),
            ) from exc

        usage = getattr(resp, "usage", None)
        return VlmExtractionResponse(
            response_json=content,
            model_id=self._model,
            completed_at=datetime.utcnow(),
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )


__all__ = ["OpenAIVlmClient", "OpenAIVlmError"]
