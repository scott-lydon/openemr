"""Section-aware chunker for the Retrieval Augmented Generation (RAG)
corpus.

A clinical guideline document is structured: a recommendation block
("Adults aged 35–70 years with overweight or obesity should be screened
for prediabetes...") is one self-contained unit. Splitting that block
across two chunks costs both halves their meaning.

The chunker:

1. Walks the document section by section (the input format is markdown
   with H2/H3 headings; the chunker treats each leaf section as a unit).
2. Within each section, accumulates paragraphs into a chunk until the
   target token count is reached.
3. When a paragraph would push the chunk past the target by more than
   the overlap allowance, starts a new chunk.
4. Each new chunk inherits the previous chunk's last 64 tokens as
   overlap so the boundary is not a hard cut.
5. A recommendation block (a paragraph starting with "Recommendation:"
   or "We recommend") never splits — it is emitted as its own chunk.

Token counting:

- ``tiktoken`` if available; otherwise a whitespace-split fallback that
  approximates 4 characters per token (the OpenAI rule of thumb).
- The fallback is intentionally rough; tests assert chunk size within
  a 25% tolerance of the target so the rough count does not break.

Domain tagging:

- A small dictionary of regex patterns assigns ``DomainTag`` values to
  chunks. A chunk can carry multiple tags. The list is stable (no
  randomized ordering) so a re-chunk produces deterministic tag lists.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final, Iterable

from sidecar.rag.types import DomainTag


# Tunables. The 512 / 64 split is the standard chunk-size target for
# embedders of the text-embedding-3-large family.
DEFAULT_TARGET_TOKENS: Final[int] = 512
DEFAULT_OVERLAP_TOKENS: Final[int] = 64
HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
RECOMMENDATION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:Recommendation|We recommend|Strong recommendation|Weak recommendation)",
    re.IGNORECASE,
)

# Domain tag regex map. Order does not matter (we collect all matches).
# Patterns are intentionally loose — a chunk that mentions "diabetes"
# anywhere gets tagged ``diabetes``; the retriever's filter is the strict
# layer.
_DOMAIN_PATTERNS: Final[tuple[tuple[DomainTag, re.Pattern[str]], ...]] = (
    (DomainTag.DIABETES, re.compile(r"\b(diabetes|HbA1c|glycemic|insulin)\b", re.IGNORECASE)),
    (DomainTag.HYPERTENSION, re.compile(r"\b(hypertens(ion|ive)|blood pressure|BP)\b", re.IGNORECASE)),
    (DomainTag.LIPIDS, re.compile(r"\b(LDL|HDL|cholesterol|statin|lipid)\b", re.IGNORECASE)),
    (DomainTag.SCREENING, re.compile(r"\b(screen(ing)?|preventive|USPSTF)\b", re.IGNORECASE)),
    (DomainTag.IMMUNIZATION, re.compile(r"\b(vaccine|immunization|booster)\b", re.IGNORECASE)),
    (DomainTag.GOUT, re.compile(r"\b(gout|uric acid|crystal arthropathy|allopurinol)\b", re.IGNORECASE)),
    (DomainTag.OSTEOPOROSIS, re.compile(r"\b(osteoporosis|bone density|DXA|bisphosphonate)\b", re.IGNORECASE)),
    (DomainTag.MENTAL_HEALTH, re.compile(r"\b(depression|anxiety|PHQ-9|GAD-7|suicide)\b", re.IGNORECASE)),
    (DomainTag.CARDIOVASCULAR, re.compile(r"\b(cardiovascular|coronary|stroke|MI|myocardial)\b", re.IGNORECASE)),
    (DomainTag.ONCOLOGY, re.compile(r"\b(cancer|tumor|oncolog|carcinoma)\b", re.IGNORECASE)),
)


@dataclass(frozen=True)
class ChunkInput:
    """Input to ``chunk_document``.

    ``markdown`` is the document body (UTF-8 text). ``source_id`` and
    ``anchor_url`` are passed through onto every output chunk.
    """

    source_id: str
    anchor_url: str
    license_url: str
    markdown: str


@dataclass(frozen=True)
class RawChunk:
    """A chunked block before the embedder runs.

    ``embedding_model`` is set later by the indexer; this DTO is the
    pre-embedding shape.
    """

    chunk_id: str
    source_id: str
    section_path: str
    anchor_url: str
    license_url: str
    domain_tags: list[DomainTag]
    text: str


def chunk_document(
    inp: ChunkInput,
    *,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[RawChunk]:
    """Split ``inp.markdown`` into chunks. Section-aware, recommendation
    -aware, with overlap.

    Returns a list in document order. Empty input returns an empty list
    (not an error — an empty document just produces no chunks).
    """
    if target_tokens < 64:
        raise ValueError(
            f"target_tokens={target_tokens} too small; min 64 for "
            "meaningful chunks."
        )
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError(
            f"overlap_tokens={overlap_tokens} out of range "
            f"[0, target_tokens)={target_tokens}"
        )
    if not inp.markdown.strip():
        return []

    sections = list(_split_into_sections(inp.markdown))

    chunks: list[RawChunk] = []
    for section_path, section_text in sections:
        chunks.extend(
            _chunk_section(
                inp=inp,
                section_path=section_path,
                section_text=section_text,
                target_tokens=target_tokens,
                overlap_tokens=overlap_tokens,
            )
        )
    return chunks


def _split_into_sections(markdown: str) -> Iterable[tuple[str, str]]:
    """Yield ``(section_path, body)`` for each H2/H3 leaf section.

    H1 is treated as document title and not yielded. A document with no
    H2 still yields ``("", body)`` so the chunker has something to work
    with.
    """
    lines = markdown.splitlines()
    h1: str | None = None
    h2: str | None = None
    h3: str | None = None
    buffer: list[str] = []

    def flush() -> tuple[str, str]:
        section_path = " > ".join(part for part in (h1, h2, h3) if part)
        body = "\n".join(buffer).strip()
        return section_path, body

    yielded_any = False
    for line in lines:
        match = HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            heading = match.group(2).strip()
            if buffer:
                section_path, body = flush()
                if body:
                    yield section_path, body
                    yielded_any = True
                buffer.clear()
            if level == 1:
                h1, h2, h3 = heading, None, None
            elif level == 2:
                h2, h3 = heading, None
            elif level == 3:
                h3 = heading
            else:
                # H4+ folded into the parent H3.
                h3 = (h3 + " > " + heading) if h3 else heading
            continue
        buffer.append(line)

    if buffer:
        section_path, body = flush()
        if body:
            yield section_path, body
            yielded_any = True

    if not yielded_any:
        # Document had no headings at all; emit one chunk-friendly section.
        body = markdown.strip()
        if body:
            yield "", body


def _chunk_section(
    *,
    inp: ChunkInput,
    section_path: str,
    section_text: str,
    target_tokens: int,
    overlap_tokens: int,
) -> list[RawChunk]:
    """Chunk one leaf section with overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", section_text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[RawChunk] = []
    current: list[str] = []
    current_tokens = 0

    def emit() -> None:
        nonlocal current, current_tokens
        if not current:
            return
        text = "\n\n".join(current).strip()
        chunks.append(_make_raw_chunk(inp, section_path, text))
        # Carry over the last ``overlap_tokens`` worth of text into the
        # next chunk's prefix to soften the boundary.
        carry_paragraphs = _tail_paragraphs(current, overlap_tokens)
        current = list(carry_paragraphs)
        current_tokens = sum(_count_tokens(p) for p in current)

    for paragraph in paragraphs:
        # Recommendation blocks always emit alone so a clinical
        # recommendation is never split mid-sentence.
        if RECOMMENDATION_RE.match(paragraph):
            if current:
                emit()
            chunks.append(_make_raw_chunk(inp, section_path, paragraph))
            current.clear()
            current_tokens = 0
            continue

        para_tokens = _count_tokens(paragraph)
        if current_tokens + para_tokens > target_tokens and current:
            emit()
        current.append(paragraph)
        current_tokens += para_tokens

    if current:
        text = "\n\n".join(current).strip()
        chunks.append(_make_raw_chunk(inp, section_path, text))

    return chunks


def _tail_paragraphs(paragraphs: list[str], overlap_tokens: int) -> list[str]:
    """Return the trailing paragraphs whose combined token count is
    closest to ``overlap_tokens`` without exceeding it."""
    if overlap_tokens <= 0 or not paragraphs:
        return []
    accumulated = 0
    selected: list[str] = []
    for para in reversed(paragraphs):
        para_tokens = _count_tokens(para)
        if accumulated + para_tokens > overlap_tokens and selected:
            break
        selected.insert(0, para)
        accumulated += para_tokens
        if accumulated >= overlap_tokens:
            break
    return selected


def _count_tokens(text: str) -> int:
    """Approximate token count.

    Uses ``tiktoken`` when available (cl100k_base encoding), otherwise
    falls back to ``len(text) // 4`` rounded up, the industry-standard
    rule of thumb for English text against the BPE family.
    """
    try:
        import tiktoken
    except ImportError:
        return max(1, (len(text) + 3) // 4)
    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return max(1, (len(text) + 3) // 4)


def _make_raw_chunk(
    inp: ChunkInput,
    section_path: str,
    text: str,
) -> RawChunk:
    """Build a ``RawChunk`` with a deterministic id.

    The id is the hex digest of
    ``sha256(source_id + section_path + text[:128])``. Truncating the
    text prefix keeps the id stable when a downstream re-chunk produces
    a slightly longer text (for example because the overlap landed
    differently).
    """
    digest = hashlib.sha256(
        f"{inp.source_id}|{section_path}|{text[:128]}".encode("utf-8")
    ).hexdigest()[:32]
    return RawChunk(
        chunk_id=digest,
        source_id=inp.source_id,
        section_path=section_path,
        anchor_url=inp.anchor_url,
        license_url=inp.license_url,
        domain_tags=detect_domain_tags(text),
        text=text,
    )


def detect_domain_tags(text: str) -> list[DomainTag]:
    """Return every ``DomainTag`` that the text matches.

    Order is the enum's declaration order, deterministic across runs.
    Returns an empty list when no patterns match; the chunk is still
    indexable (with no domain restrictions applying to it).
    """
    return [tag for tag, pattern in _DOMAIN_PATTERNS if pattern.search(text)]


__all__ = [
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_TARGET_TOKENS",
    "ChunkInput",
    "RawChunk",
    "chunk_document",
    "detect_domain_tags",
]
