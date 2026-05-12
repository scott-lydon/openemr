"""Unit tests for selective FHIR shard retrieval.

The chat handler computes a :class:`ShardSelection` per request from
the purpose and (optional) message text. These tests pin the policy so
a future change cannot silently regress to the legacy "fan out
everything" behaviour, which was the explicit focus of the v2 review
feedback.
"""

from __future__ import annotations

import pytest

from sidecar.snapshot.fhir_client import DEFAULT_RESOURCE_QUERIES
from sidecar.snapshot.shard_selection import (
    ALL_SHARDS,
    ShardSelection,
    refine_shards_for_message,
    select_shards,
    select_shards_for_purpose,
)


_ALL_NAMES = {name for name, _ in DEFAULT_RESOURCE_QUERIES}


def test_default_resource_queries_match_all_shards() -> None:
    """``ALL_SHARDS`` must include every name in ``DEFAULT_RESOURCE_QUERIES``.

    If a future shard is added to the FHIR client without updating the
    selector, the keyword refinement table can no longer reach it.
    """
    assert ALL_SHARDS.names == frozenset(_ALL_NAMES)


def test_diagnostic_cross_check_pulls_every_shard() -> None:
    """Use Case A's pairwise comparator depends on every documented finding."""
    sel = select_shards_for_purpose("diagnostic_cross_check")
    assert sel.names == frozenset(_ALL_NAMES)


def test_chart_error_scan_drops_documents_by_default() -> None:
    """Use Case B does not need the documents shard at baseline."""
    sel = select_shards_for_purpose("chart_error_scan")
    assert "documents" not in sel
    assert "conditions" in sel
    assert "medications" in sel
    assert "allergies" in sel


def test_follow_up_question_starts_minimal() -> None:
    """A bare follow-up only pulls problem list, meds, and allergies."""
    sel = select_shards_for_purpose("follow_up_question")
    assert sel.names == frozenset({"conditions", "medications", "allergies"})
    assert "labs" not in sel
    assert "documents" not in sel


def test_unknown_purpose_raises() -> None:
    """Typo'd purpose names must fail loud, not silently fan-out everything."""
    with pytest.raises(ValueError, match="unknown chat purpose"):
        select_shards_for_purpose("not_a_real_purpose")  # type: ignore[arg-type]


def test_message_keyword_pulls_labs() -> None:
    """A follow-up about HbA1c retrieves the labs shard."""
    sel = select_shards("follow_up_question", "what is her latest HbA1c?")
    assert "labs" in sel
    assert "conditions" in sel  # baseline still present


def test_message_keyword_pulls_documents_for_consult() -> None:
    """A follow-up that mentions a consult letter retrieves DocumentReference."""
    sel = select_shards(
        "follow_up_question",
        "did the orthopedist's note recommend physical therapy or injections?",
    )
    assert "documents" in sel


def test_message_keyword_pulls_procedures_for_colonoscopy() -> None:
    """The colonoscopy follow-up shown in USERS.md §3.3 retrieves procedures."""
    sel = select_shards("follow_up_question", "when was her last colonoscopy?")
    assert "procedures" in sel


def test_refinement_is_additive_only() -> None:
    """Keyword matches must add shards, never remove the baseline ones."""
    base = select_shards_for_purpose("chart_error_scan")
    refined = refine_shards_for_message(base, "scan the radiology notes")
    # Baseline shards still present.
    assert base.names <= refined.names
    # Documents got added.
    assert "documents" in refined


def test_empty_message_returns_baseline() -> None:
    """A whitespace-only message must not pull anything beyond baseline."""
    base = select_shards_for_purpose("follow_up_question")
    assert refine_shards_for_message(base, "   ").names == base.names
    assert refine_shards_for_message(base, None).names == base.names


def test_unknown_message_keyword_returns_baseline() -> None:
    """A message with no recognised keyword is safe — baseline is still pulled."""
    base = select_shards_for_purpose("follow_up_question")
    refined = refine_shards_for_message(base, "the patient said ouch")
    # Important: do not silently expand to ALL_SHARDS — under-fetching
    # is recoverable, over-fetching defeats the optimisation.
    assert refined.names == base.names


def test_ordered_preserves_default_order() -> None:
    """``ordered`` must walk shards in the canonical fan-out order."""
    sel = ShardSelection(names=frozenset({"labs", "conditions", "medications"}))
    names_in_order = [name for name, _ in sel.ordered]
    expected = [
        name for name, _ in DEFAULT_RESOURCE_QUERIES if name in sel.names
    ]
    assert names_in_order == expected


def test_union_merges_shard_sets() -> None:
    """``union`` is set union — useful for "follow-up plus error scan" combos."""
    a = ShardSelection(names=frozenset({"conditions"}))
    b = ShardSelection(names=frozenset({"labs"}))
    assert a.union(b).names == frozenset({"conditions", "labs"})
