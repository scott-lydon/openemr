"""Build the evidence packet from retrieved snippets and chart facts.

A pure function, deliberately. The packet builder takes the state's
``snippets`` (RAG hits) plus any worker-produced raw claims and
produces the consolidated list of ``ClinicalClaim`` the verifier will
score. No LLM call here — we want this step to be fast, deterministic,
and unit-test-friendly.

What the builder does:

1. **Enrich claims with citations.** Each retrieved snippet contributes
   a citation id (``snippet.chunk_id``); claims produced by upstream
   workers carry their own citation lists, which we leave intact.
2. **Add baseline guideline claims.** When the supervisor's intent was
   ``GUIDELINE_LOOKUP`` and there is at least one snippet, the builder
   emits one ``ClinicalClaim`` per snippet so the verifier can
   downstream check each citation. The text is a short summary line so
   the formatter has something to display.
3. **Deduplicate.** Two claims with identical text are merged; the
   merged claim carries the union of citations.

The builder does not make clinical judgments; it stitches together the
inputs the upstream nodes produced.
"""

from __future__ import annotations

from sidecar.agents.w2.state import ClinicalClaim, GraphState, IntentKind


def build_evidence_packet(state: GraphState) -> list[ClinicalClaim]:
    """Return the list of claims the verifier should evaluate.

    Pure: same input -> same output. No mutation of ``state``.
    """
    claims: list[ClinicalClaim] = list(state.raw_claims)

    if state.intent_kind is IntentKind.GUIDELINE_LOOKUP and state.snippets:
        for snippet in state.snippets:
            text = _summarize_snippet_text(snippet.text)
            claims.append(
                ClinicalClaim(text=text, citations=[snippet.chunk_id])
            )

    return _dedupe_claims(claims)


def _summarize_snippet_text(text: str) -> str:
    """Trim the snippet text to the first sentence (or 240 chars).

    Keeps the displayed claim short. The full snippet remains accessible
    via the citation chip click-through, which Phase 6 implements.
    """
    body = text.strip()
    cutoff = body.find(". ")
    if 0 < cutoff < 240:
        return body[: cutoff + 1]
    return body[:240]


def _dedupe_claims(claims: list[ClinicalClaim]) -> list[ClinicalClaim]:
    """Merge claims with identical text; union their citations.

    Stable: preserves first occurrence order. Non-trivial because each
    ``ClinicalClaim`` is frozen Pydantic — we rebuild the merged claim
    rather than mutating in place.
    """
    by_text: dict[str, list[str]] = {}
    order: list[str] = []
    for claim in claims:
        existing = by_text.get(claim.text)
        if existing is None:
            by_text[claim.text] = list(claim.citations)
            order.append(claim.text)
        else:
            for cid in claim.citations:
                if cid not in existing:
                    existing.append(cid)

    return [
        ClinicalClaim(text=text, citations=by_text[text])
        for text in order
    ]


__all__ = ["build_evidence_packet"]
