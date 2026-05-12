"""Reciprocal Rank Fusion (RRF).

Given two ranked lists (typically a sparse BM25 list and a dense
vector list), produce one fused list whose score for each unique item
is::

    score(item) = sum_over_lists( 1 / (k + rank_in_list) )

where ``rank_in_list`` is 1-based. Items missing from a list contribute
zero from that list.

Why RRF rather than weighted-sum:

- RRF needs no normalization between the two scoring systems. BM25's
  raw scores and cosine similarities are on incomparable scales; a
  sum-with-weights tuning needs per-corpus calibration. RRF is
  parameter-free except for ``k``, which is robust across corpora.
- The standard ``k=60`` from the original RRF paper (Cormack 2009)
  performs well on every corpus we have benchmarked. We expose ``k``
  so a future tuning experiment can sweep it.

Output:

- The fused list is sorted descending by RRF score.
- Items absent from both inputs are absent from the output.
- The function is order-stable: equal scores preserve the order of the
  first list, then the second.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Generic, TypeVar


DEFAULT_K: Final[int] = 60


T = TypeVar("T")


def reciprocal_rank_fuse(
    *lists: Sequence[T],
    k: int = DEFAULT_K,
    key=lambda item: item,
) -> list[T]:
    """Fuse arbitrarily many ranked lists by Reciprocal Rank Fusion.

    ``key`` extracts the dedup identity from each item (for example,
    ``lambda hit: hit.chunk_id``). Items in different input lists with
    the same key are merged into one output entry; the output entry is
    the first occurrence's representation.

    Empty inputs return ``[]``. ``k`` must be positive (a non-positive
    ``k`` would invert the rank's contribution).
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")

    scores: dict[object, float] = {}
    representatives: dict[object, T] = {}
    first_seen: dict[object, int] = {}
    for list_index, ranked in enumerate(lists):
        for rank, item in enumerate(ranked, start=1):
            ident = key(item)
            scores[ident] = scores.get(ident, 0.0) + 1.0 / (k + rank)
            if ident not in representatives:
                representatives[ident] = item
                first_seen[ident] = list_index * 1_000_000 + rank

    sorted_idents = sorted(
        representatives,
        key=lambda ident: (-scores[ident], first_seen[ident]),
    )
    return [representatives[ident] for ident in sorted_idents]


__all__ = ["DEFAULT_K", "reciprocal_rank_fuse"]
