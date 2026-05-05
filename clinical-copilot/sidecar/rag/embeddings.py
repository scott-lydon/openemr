"""Embedding client protocol and stub.

The retriever needs a query embedding to run vector search. This module
defines the protocol and a deterministic stub used by unit tests.

Production uses OpenAI's ``text-embedding-3-large`` halved with
Matryoshka to 1024 dimensions (the dimension is recorded on every
``guideline_chunks`` row so a future re-embedding migration is
auditable). The protocol hides the provider so a swap to a different
embedder is one file's worth of code.

Stub strategy:

- ``StubEmbedder`` returns a deterministic vector seeded from a hash
  of the input text. The vectors are unit-normalized so cosine
  similarity is monotonic in match quality between two stub vectors.
- The dimension is fixed at 16 in the stub (small for fast tests).
  The production wiring uses 1024.
"""

from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass
from typing import Final, Protocol


DEFAULT_PRODUCTION_DIM: Final[int] = 1024
STUB_DIM: Final[int] = 16


class Embedder(Protocol):
    """Protocol every embedder implementation honors."""

    @property
    def model(self) -> str:
        ...

    @property
    def dimension(self) -> int:
        ...

    async def embed_query(self, query: str) -> list[float]:
        ...


@dataclass(frozen=True)
class StubEmbedder:
    """Deterministic stub for unit tests.

    The vector for a given input is produced by hashing the text and
    interpreting the digest as a series of floats. Identical text →
    identical vector; near-duplicate text → near-identical vector;
    unrelated text → near-orthogonal vector. Good enough for unit tests
    that assert "this query retrieves this chunk".
    """

    model: str = "stub-embedder-v1"
    dimension: int = STUB_DIM

    async def embed_query(self, query: str) -> list[float]:
        if not query:
            raise ValueError("StubEmbedder.embed_query received empty query")
        return embed_text_deterministic(query, dimension=self.dimension)


def embed_text_deterministic(text: str, *, dimension: int) -> list[float]:
    """Produce a deterministic unit-norm vector from ``text``.

    Exposed so the in-memory test index can pre-compute embeddings for
    its chunks the same way the stub embedder embeds queries — this is
    what makes "embed query, find matching chunk" actually work in
    unit tests.
    """
    if dimension <= 0:
        raise ValueError(f"dimension must be positive, got {dimension}")

    # Generate enough digest bytes to fill the requested dimension.
    needed_bytes = dimension * 4  # 4 bytes per float
    accumulated = b""
    counter = 0
    seed = text.encode("utf-8")
    while len(accumulated) < needed_bytes:
        accumulated += hashlib.sha256(seed + counter.to_bytes(8, "little")).digest()
        counter += 1

    raw = struct.unpack(f"{dimension}I", accumulated[:needed_bytes])
    # Map each unsigned 32-bit int to (-1, 1).
    floats = [(value / 0xFFFFFFFF) * 2.0 - 1.0 for value in raw]
    norm = math.sqrt(sum(f * f for f in floats)) or 1.0
    return [f / norm for f in floats]


__all__ = [
    "DEFAULT_PRODUCTION_DIM",
    "Embedder",
    "STUB_DIM",
    "StubEmbedder",
    "embed_text_deterministic",
]
