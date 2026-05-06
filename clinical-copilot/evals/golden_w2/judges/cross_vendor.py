"""Cross-vendor judge: when the extractor is OpenAI, the judge is Anthropic.

Why a different vendor:

- Pass/fail evaluation is a separate failure mode from extraction. Two
  models from the same vendor share blind spots (training distribution,
  prompt-injection susceptibilities, calibration biases). A
  cross-vendor split halves the chance that the extractor fooled the
  judge.

The judge consumes a strict JSON-shaped prompt and emits a strict
JSON-shaped verdict. Pydantic parses the verdict; a malformed verdict
is a build failure, not a silent miss.

Two implementations:

- ``ClaudeJudge`` — production. Calls Anthropic's messages API. Reads
  ``ANTHROPIC_API_KEY`` from the environment.
- ``StubJudge`` — deterministic substitute used by unit tests and the
  CI smoke run. The judge always returns ``passed=True`` unless the
  test wires up a different verdict.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Final, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


logger = logging.getLogger(__name__)


JUDGE_PROMPT_VERSION: Final[str] = "judge.cross_vendor.v1"
ANTHROPIC_MODEL: Final[str] = "claude-haiku-4-5-20251001"


class JudgeVerdict(BaseModel):
    """Strict shape the judge model must return."""

    model_config = ConfigDict(extra="forbid", strict=True)

    passed: bool
    rubric_category: str
    reasons: list[str] = Field(default_factory=list)
    judge_prompt_version: str


class JudgeError(Exception):
    """The judge could not produce a verdict."""


class Judge(Protocol):
    async def judge(
        self,
        *,
        case_id: str,
        rubric_category: str,
        user_question: str,
        agent_response: str,
        expected_summary: str,
    ) -> JudgeVerdict:
        ...


@dataclass
class StubJudge:
    """Deterministic test substitute.

    ``verdicts`` maps ``case_id`` -> ``JudgeVerdict``. A missing case
    raises ``KeyError`` so a forgotten fixture fails loudly. Use the
    ``default_pass`` flag to make the stub pass everything (useful for
    unit testing the harness without per-case fixture wiring).
    """

    verdicts: dict[str, JudgeVerdict] = field(default_factory=dict)
    default_pass: bool = False

    async def judge(
        self,
        *,
        case_id: str,
        rubric_category: str,
        user_question: str,
        agent_response: str,
        expected_summary: str,
    ) -> JudgeVerdict:
        if case_id in self.verdicts:
            return self.verdicts[case_id]
        if self.default_pass:
            return JudgeVerdict(
                passed=True,
                rubric_category=rubric_category,
                reasons=["stub default pass"],
                judge_prompt_version=JUDGE_PROMPT_VERSION,
            )
        raise KeyError(
            f"StubJudge: no verdict for case {case_id!r}. Either add "
            "one to .verdicts or set default_pass=True."
        )


@dataclass
class ClaudeJudge:
    """Production Anthropic Claude Haiku judge."""

    api_key: str | None = None
    model: str = ANTHROPIC_MODEL
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "ClaudeJudge":
        return cls(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    async def judge(
        self,
        *,
        case_id: str,
        rubric_category: str,
        user_question: str,
        agent_response: str,
        expected_summary: str,
    ) -> JudgeVerdict:
        if not self.api_key:
            raise JudgeError(
                "ANTHROPIC_API_KEY is not set; cannot run the cross-vendor "
                "judge. Set the secret in CI's environment or use --judge=stub."
            )

        prompt = _build_prompt(
            case_id=case_id,
            rubric_category=rubric_category,
            user_question=user_question,
            agent_response=agent_response,
            expected_summary=expected_summary,
        )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds)) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 1024,
                        "system": _SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
        except httpx.RequestError as exc:
            raise JudgeError(
                f"Anthropic network error: {type(exc).__name__}: {exc!s}"
            ) from exc

        if response.status_code != 200:
            raise JudgeError(
                f"Anthropic returned status={response.status_code} "
                f"body[:512]={response.text[:512]!r}"
            )
        try:
            payload = response.json()
            text_blocks = payload.get("content", [])
            text = next(
                (b.get("text", "") for b in text_blocks if b.get("type") == "text"),
                "",
            )
            verdict_payload = json.loads(text)
        except (ValueError, KeyError) as exc:
            raise JudgeError(
                f"Could not parse Anthropic response: {type(exc).__name__}: "
                f"{exc!s}; preview={response.text[:200]!r}"
            ) from exc
        try:
            return JudgeVerdict.model_validate(verdict_payload, strict=False)
        except ValidationError as exc:
            raise JudgeError(f"Judge verdict failed schema: {exc!s}") from exc


_SYSTEM_PROMPT = """You are a strict, evidence-grounded clinical eval judge.

Return STRICT JSON with exactly these keys:
- passed: boolean (true if the agent_response satisfies the rubric_category for the user_question and expected_summary, false otherwise).
- rubric_category: echo the input string.
- reasons: array of short strings (<=120 chars each) explaining the verdict.
- judge_prompt_version: the prompt version string the harness sent.

You judge ONE rubric_category per call. Do not consider any other rubric. Do not produce text outside the JSON.
"""


def _build_prompt(
    *,
    case_id: str,
    rubric_category: str,
    user_question: str,
    agent_response: str,
    expected_summary: str,
) -> str:
    return (
        f"case_id: {case_id}\n"
        f"rubric_category: {rubric_category}\n"
        f"user_question: {user_question}\n"
        f"expected_summary: {expected_summary}\n"
        f"agent_response: {agent_response}\n"
        f"judge_prompt_version: {JUDGE_PROMPT_VERSION}\n"
    )


__all__ = [
    "ANTHROPIC_MODEL",
    "ClaudeJudge",
    "JUDGE_PROMPT_VERSION",
    "Judge",
    "JudgeError",
    "JudgeVerdict",
    "StubJudge",
]
