"""LLM synthesizer for the Week 2 chat surface.

Replaces the previous "dump every claim verbatim" formatter for chat
turns. Architecturally identical to the Week 1 ``run_follow_up`` path
(``sidecar/agent/follow_up.py``) but operates on a *verified*
``ResponsePacket`` rather than a ``PatientSnapshot``:

- Inputs:  user_question, prior_turns, verified claims (post-verifier),
           supporting evidence snippets (RAG hits or chart facts).
- Output:  one synthesized natural-language ``answer`` string that
           addresses the user's actual question, plus the list of
           citation ids the synthesizer chose to anchor to.

Why a synthesizer rather than fixing the formatter:

- The formatter is a pure markdown renderer; it cannot reason about
  the user's question. Adding LLM calls inside it would entangle two
  concerns (rendering vs reasoning) that should stay separate.
- The verifier already enforces "every surviving claim has a valid
  citation" — the synthesizer can only re-arrange and re-explain
  claims that already passed verification, so it cannot fabricate
  un-cited facts. It can however *omit* claims that don't help answer
  the question, which is exactly what we want for the "tell me more"
  follow-up case.

Failure handling:

- If the synthesizer call fails (network, schema, API key missing) we
  fall back to the dumb formatter so the chat keeps working. Every
  failure path raises a typed ``SynthesizerError`` first so the chat
  layer can surface a one-line diagnostic and continue.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Iterable

from pydantic import BaseModel, Field

from sidecar.agent.conversation import ConversationTurn, render_history_for_prompt
from sidecar.agents.w2.state import ClinicalClaim, ResponsePacket
from sidecar.config import Settings, get_settings
from sidecar.rag.types import EvidenceSnippet


logger = logging.getLogger(__name__)


# Bumped on any prompt edit — the audit log records this so a regression
# after a prompt change is attributable.
W2_SYNTHESIZER_PROMPT_VERSION = "w2.synth.v1"


# ─── Errors ──────────────────────────────────────────────────────────


class SynthesizerError(RuntimeError):
    """Base class for every failure inside the synthesizer.

    Carries a stable ``code`` the chat layer can log + a debug hint the
    operator runbook references. Every concrete failure mode is a
    subclass with a pre-set code so ``except SynthesizerError as exc`` in
    chat_w2 captures one place's worth of error reporting.
    """

    code: str = "synthesizer_error"


class SynthesizerConfigError(SynthesizerError):
    code = "synthesizer_config_missing"


class SynthesizerProviderError(SynthesizerError):
    code = "synthesizer_provider_failed"


class SynthesizerSchemaError(SynthesizerError):
    code = "synthesizer_schema_invalid"


# ─── Output schema ───────────────────────────────────────────────────


class SynthesizedAnswer(BaseModel):
    """Structured output from the synthesizer call.

    Fields:

    - ``answer`` — the natural-language response shown to the
      clinician. 1-6 sentences. References the claims by their
      citation chips (e.g. "[1]") so the existing chat UI's chip
      handler keeps working.
    - ``cited_indices`` — the 1-based positions in the claim list the
      answer actually used. The caller renders these as the citation
      footer; claims the synthesizer chose to ignore are not surfaced.
      Empty when the answer is a refusal.
    - ``data_gaps`` — explicit gaps the synthesizer identified
      ("snapshot does not include uric acid value"). Surfaced inline
      to the user so they don't mistake silence for "no gap".
    - ``verdict`` — ``"answered" | "answered_with_gaps" |
      "insufficient_data"``. The chat layer shows this color-coded.
    """

    answer: str = Field(max_length=2000)
    cited_indices: list[int] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list, max_length=10)
    verdict: str = Field(default="answered")


# ─── Inputs to the synthesizer ───────────────────────────────────────


@dataclass(frozen=True)
class SynthesizerInputs:
    """Everything the synthesizer needs to produce one answer.

    Frozen so a future refactor that wants to memoize identical inputs
    (e.g. the same question asked twice in a row) gets a hashable key.
    """

    user_question: str
    prior_turns: tuple[ConversationTurn, ...]
    response: ResponsePacket
    snippets: tuple[EvidenceSnippet, ...]


# ─── System prompt ───────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You are a clinical co-pilot helping a primary-care physician at the bedside.
The physician just asked you a question about ONE patient. You are given:

* The most recent conversation turns (so referents like "those" or
  "her" resolve correctly).
* A list of FACTS, each one a verified claim with one or more citation
  ids. A separate evidence-pipeline already enforced that every fact
  cites a real row in the chart or a real chunk in the guideline corpus
  - you may not fabricate facts beyond this list.

Your job:

1. Answer the physician's CURRENT question in plain English, in 1-6
   sentences. Be specific. Synthesize across multiple facts when the
   question demands it ("what should I do about her diabetes?" should
   combine the diagnosis fact with relevant guideline chunks).
2. Reference the facts inline using their 1-based index in square
   brackets, e.g. "...her HbA1c is elevated [1] and metformin is the
   recommended first-line therapy [3][4]."
3. If the FACTS do not contain enough information to answer, say so
   explicitly in ``data_gaps`` and set ``verdict`` to
   ``insufficient_data``. Do not invent a value, date, or
   recommendation that is not in the facts.
4. If the facts partially answer the question, set ``verdict`` to
   ``answered_with_gaps`` and list the gaps.
5. ``cited_indices`` MUST list every fact index your answer
   references. Do NOT cite a fact you did not use.

Calibrate confidence to what the facts actually show. The physician
trusts the citations more than confident-sounding prose.
"""


# ─── Public entry point ──────────────────────────────────────────────


async def synthesize(
    inputs: SynthesizerInputs,
    *,
    settings: Settings | None = None,
) -> SynthesizedAnswer:
    """Run the synthesizer and return the structured answer.

    Routing (the previous implementation forced the mock path whenever
    ``COPILOT_ALLOW_MOCK=true``, which meant a real OpenAI key was
    ignored just because mock mode was on for upstream services like the
    document store and queue. The synthesizer is downstream pure prose —
    it does not reach Postgres, ClamAV, or the queue — so it is safe to
    use the real LLM here even while the rest of the stack is mocked):

    * Use OpenAI whenever a key is configured. ``COPILOT_ALLOW_MOCK`` no
      longer downgrades the synthesizer.
    * Set ``COPILOT_FORCE_MOCK_SYNTHESIZER=true`` to override and force
      the deterministic mock (for offline CI, prompt-stability tests, or
      when you intentionally want to bypass the API for cost reasons).
    * Without an OpenAI key the mock is the only option.

    Raises ``SynthesizerError`` on every failure mode (config missing,
    network, schema invalid). The caller falls back to the dumb formatter.
    """
    cfg = settings or get_settings()
    has_key = bool(cfg.openai_api_key)
    force_mock = (
        os.environ.get("COPILOT_FORCE_MOCK_SYNTHESIZER", "").lower() == "true"
    )

    if force_mock or not has_key:
        return _mock_synthesize(inputs)

    return await _openai_synthesize(inputs, cfg)


# ─── Prompt rendering ────────────────────────────────────────────────


def _render_facts_block(claims: Iterable[ClinicalClaim]) -> str:
    """Render the verified claims as a numbered fact list."""
    lines: list[str] = []
    for index, claim in enumerate(claims, start=1):
        cite_str = ", ".join(claim.citations) if claim.citations else "(no citation)"
        lines.append(f"[{index}] {claim.text}  <cite={cite_str}>")
    return "\n".join(lines) if lines else "(none — no verified facts available)"


def _render_snippets_block(snippets: Iterable[EvidenceSnippet]) -> str:
    """Render raw RAG snippets as a supplementary context block.

    These are the same snippets that produced the claims, but with the
    full snippet text rather than the truncated claim summary. The
    synthesizer can quote them more faithfully because it sees the full
    sentence the corpus loader stored.
    """
    lines: list[str] = []
    for snippet in snippets:
        body = snippet.text.strip().replace("\n", " ")
        if len(body) > 600:
            body = body[:600] + "…"
        lines.append(
            f"- chunk_id={snippet.chunk_id}  source={snippet.source_id} "
            f"section={snippet.section}\n  text: {body}"
        )
    return "\n".join(lines) if lines else "(none)"


def _build_user_prompt(inputs: SynthesizerInputs) -> str:
    """Assemble the user-message body for the synthesizer call."""
    parts: list[str] = []
    history = render_history_for_prompt(inputs.prior_turns)
    if history:
        parts.append("CONVERSATION_SO_FAR:\n" + history)
    parts.append(f"CURRENT_QUESTION: {inputs.user_question.strip()}")
    parts.append(
        "FACTS (verified; cite by 1-based index):\n"
        + _render_facts_block(inputs.response.claims)
    )
    parts.append(
        "FULL_EVIDENCE_TEXT (background context, optional):\n"
        + _render_snippets_block(inputs.snippets)
    )
    if inputs.response.refusal_reason:
        parts.append(f"REFUSAL_REASON_FROM_VERIFIER: {inputs.response.refusal_reason}")
    return "\n\n".join(parts)


# ─── OpenAI provider ─────────────────────────────────────────────────


async def _openai_synthesize(
    inputs: SynthesizerInputs, settings: Settings
) -> SynthesizedAnswer:
    """One structured-output call against OpenAI for the synthesizer."""
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise SynthesizerConfigError(
            "openai package not installed; install with `pip install openai`. "
            f"Underlying ImportError: {exc!s}"
        ) from exc

    if not settings.openai_api_key:
        raise SynthesizerConfigError(
            "OPENAI_API_KEY is empty or missing. Either set the key in .env "
            "or run with COPILOT_ALLOW_MOCK=true to use the deterministic "
            "mock synthesizer."
        )

    client_kwargs: dict[str, object] = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        client_kwargs["base_url"] = settings.openai_base_url
    client = AsyncOpenAI(**client_kwargs)
    user_prompt = _build_user_prompt(inputs)

    try:
        # Prefer the SDK's pydantic-native parse() (strict mode).
        beta_chat = getattr(getattr(client, "beta", None), "chat", None)
        if beta_chat is not None and hasattr(beta_chat.completions, "parse"):
            resp = await client.beta.chat.completions.parse(
                model=settings.openai_model,
                temperature=0.0,
                response_format=SynthesizedAnswer,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            parsed = resp.choices[0].message.parsed
            if parsed is None:
                raise SynthesizerSchemaError(
                    "openai parse() returned no parsed object. The model may "
                    "have refused or produced unparseable JSON. Inspect the "
                    "raw response in the launch log."
                )
            return parsed

        # Fallback for older SDKs: json_object mode + manual validation.
        resp = await client.chat.completions.create(
            model=settings.openai_model,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        _SYSTEM_PROMPT
                        + "\nReturn JSON matching this schema:\n"
                        + json.dumps(SynthesizedAnswer.model_json_schema())
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        try:
            return SynthesizedAnswer.model_validate_json(content)
        except Exception as exc:
            raise SynthesizerSchemaError(
                f"json_object response did not match SynthesizedAnswer schema: "
                f"{type(exc).__name__}: {exc!s}. Raw: {content[:300]!r}"
            ) from exc
    except SynthesizerError:
        raise
    except Exception as exc:
        raise SynthesizerProviderError(
            f"openai synthesizer call failed: {type(exc).__name__}: {exc!s}. "
            "Check OPENAI_API_KEY, model name, and network/proxy settings; "
            "see the .launch.log for the full traceback."
        ) from exc


# ─── Mock provider (deterministic, offline) ──────────────────────────


def _mock_synthesize(inputs: SynthesizerInputs) -> SynthesizedAnswer:
    """Deterministic mock: concatenates claim texts with simple framing.

    Better than the dumb formatter because it (a) frames the answer as a
    response to the question, (b) groups chart claims separately from
    guideline claims, (c) flags obvious gaps. Still keyword-driven for
    the eval suite.

    This path runs whenever COPILOT_ALLOW_MOCK=true or no OpenAI key is
    set, so even a key-less Hetzner deployment still produces an answer
    that is more useful than the prior verbatim dump.
    """
    if inputs.response.refusal_reason:
        return SynthesizedAnswer(
            answer=(
                "I cannot answer this without verified evidence. "
                + inputs.response.refusal_reason
            ),
            cited_indices=[],
            data_gaps=[],
            verdict="insufficient_data",
        )

    claims = inputs.response.claims
    if not claims:
        return SynthesizedAnswer(
            answer=(
                "The chart and guideline corpus did not surface a verified "
                "fact that addresses your question. Try a more specific "
                "question (lab name, condition, or guideline source) or "
                "attach a relevant document."
            ),
            cited_indices=[],
            data_gaps=["No verified facts surfaced for this question."],
            verdict="insufficient_data",
        )

    chart_indices: list[int] = []
    guideline_indices: list[int] = []
    for index, claim in enumerate(claims, start=1):
        if any(c.startswith("chart:") for c in claim.citations):
            chart_indices.append(index)
        else:
            guideline_indices.append(index)

    parts: list[str] = []
    if chart_indices:
        chart_clauses = [
            f"{claims[i - 1].text} [{i}]" for i in chart_indices[:6]
        ]
        parts.append(
            "From the patient's chart: " + "; ".join(chart_clauses) + "."
        )
    if guideline_indices:
        guideline_clauses = [
            f"{claims[i - 1].text} [{i}]" for i in guideline_indices[:4]
        ]
        parts.append(
            "Relevant guidance: " + " ".join(guideline_clauses)
        )
    answer = " ".join(parts) or "No content available."
    return SynthesizedAnswer(
        answer=answer,
        cited_indices=chart_indices + guideline_indices,
        data_gaps=[] if (chart_indices and guideline_indices) else [
            "Mock synthesizer in use - live OpenAI synthesis is unavailable. "
            "Set OPENAI_API_KEY and unset COPILOT_ALLOW_MOCK to enable."
        ],
        verdict="answered_with_gaps" if not (chart_indices and guideline_indices) else "answered",
    )


# ─── Rendering helper ────────────────────────────────────────────────


def render_synthesized_response(
    answer: SynthesizedAnswer,
    response: ResponsePacket,
) -> str:
    """Translate the structured answer into the markdown the chat UI expects.

    Format (intentionally close to the existing format_response output so
    the UI's parser keeps working):

        {answer text with [1] [2] markers}

        _Citations_
        [1] {citation_id}
        [2] {citation_id}

        _Data gaps_
        - {gap 1}
        - {gap 2}
    """
    lines: list[str] = [answer.answer.strip()]

    if response.claims and answer.cited_indices:
        lines.append("")
        lines.append("_Citations_")
        seen: set[int] = set()
        for index in answer.cited_indices:
            if index < 1 or index > len(response.claims) or index in seen:
                continue
            seen.add(index)
            citations_for_claim = response.claims[index - 1].citations
            cite_chip = ", ".join(citations_for_claim) if citations_for_claim else "(none)"
            lines.append(f"[{index}] {cite_chip}")

    if answer.data_gaps:
        lines.append("")
        lines.append("_Data gaps_")
        for gap in answer.data_gaps:
            lines.append(f"- {gap}")

    return "\n".join(lines).strip()


__all__ = [
    "SynthesizedAnswer",
    "SynthesizerConfigError",
    "SynthesizerError",
    "SynthesizerInputs",
    "SynthesizerProviderError",
    "SynthesizerSchemaError",
    "W2_SYNTHESIZER_PROMPT_VERSION",
    "render_synthesized_response",
    "synthesize",
]
