"""Render a verified ``ResponsePacket`` into the user-facing message.

The formatter:

- Embeds inline citation chips after each claim (``[1]``, ``[2]``,
  etc.) and lists the citations at the bottom with their source ids
  and section paths.
- Surfaces the refusal reason verbatim when the verifier flagged a
  refusal — the message tells the clinician what went wrong without
  fabricating content.
- Keeps the output small and copy-paste-friendly so the chat UI can
  render it as plain markdown.

Why a thin Jinja-free renderer rather than ``jinja2``:

- The output is structured and short. A handful of f-strings is more
  legible than a template file and removes a dependency for testing.
- A future, richer renderer can drop in here without changing the
  graph's call site (the formatter exposes a single function).
"""

from __future__ import annotations

from sidecar.agents.w2.state import ClinicalClaim, ResponsePacket


def format_response(packet: ResponsePacket) -> str:
    """Render the response packet as a markdown string.

    Returns just the message body; the chat layer wraps it in a
    role+timestamp envelope.
    """
    if packet.refusal_reason:
        return _format_refusal(packet)

    if not packet.claims:
        return packet.summary or "No verified content to share."

    citation_index: dict[str, int] = {}
    formatted_claims: list[str] = []
    for claim in packet.claims:
        markers: list[str] = []
        for cid in claim.citations:
            if cid not in citation_index:
                citation_index[cid] = len(citation_index) + 1
            markers.append(f"[{citation_index[cid]}]")
        formatted_claims.append(f"- {claim.text} {' '.join(markers)}".strip())

    body = "\n".join(formatted_claims)

    if not citation_index:
        return f"{packet.summary}\n\n{body}".strip()

    citation_lines = [
        f"[{position}] {cid}"
        for cid, position in sorted(citation_index.items(), key=lambda kv: kv[1])
    ]
    return (
        f"{packet.summary}\n\n"
        f"{body}\n\n"
        f"_Citations_\n" + "\n".join(citation_lines)
    )


def _format_refusal(packet: ResponsePacket) -> str:
    return (
        "I cannot answer this without verified evidence. "
        f"{packet.refusal_reason}"
    )


__all__ = ["format_response"]
