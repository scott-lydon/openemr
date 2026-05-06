"""Unit tests for the in-process conversation memory."""

from __future__ import annotations

import time

import pytest

from sidecar.agent.conversation import (
    ConversationMemory,
    ConversationTurn,
    render_history_for_prompt,
)


def _turn(role: str, content: str, *, ts: float | None = None,
          purpose: str = "follow_up_question") -> ConversationTurn:
    return ConversationTurn(
        role=role, content=content,
        ts_unix=ts if ts is not None else time.time(),
        purpose=purpose,
    )


def test_turns_round_trip_fifo() -> None:
    mem = ConversationMemory()
    mem.record(user_id="u1", patient_id="Patient/1", session_id="s1",
               turn=_turn("user", "first"))
    mem.record(user_id="u1", patient_id="Patient/1", session_id="s1",
               turn=_turn("assistant", "ack"))
    turns = mem.turns(user_id="u1", patient_id="Patient/1", session_id="s1")
    assert [t.content for t in turns] == ["first", "ack"]


def test_max_turns_per_session_trims_oldest_first() -> None:
    mem = ConversationMemory(max_turns_per_session=3)
    for i in range(5):
        mem.record(user_id="u1", patient_id="Patient/1", session_id="s1",
                   turn=_turn("user", f"msg{i}"))
    turns = mem.turns(user_id="u1", patient_id="Patient/1", session_id="s1")
    # Oldest two were dropped; newest three remain.
    assert [t.content for t in turns] == ["msg2", "msg3", "msg4"]


def test_sessions_are_isolated_by_user_id() -> None:
    """Cross-clinician leakage check — same patient, different clinicians."""
    mem = ConversationMemory()
    mem.record(user_id="u1", patient_id="Patient/1", session_id="s",
               turn=_turn("user", "u1 wrote this"))
    mem.record(user_id="u2", patient_id="Patient/1", session_id="s",
               turn=_turn("user", "u2 wrote this"))
    u1 = mem.turns(user_id="u1", patient_id="Patient/1", session_id="s")
    u2 = mem.turns(user_id="u2", patient_id="Patient/1", session_id="s")
    assert [t.content for t in u1] == ["u1 wrote this"]
    assert [t.content for t in u2] == ["u2 wrote this"]


def test_sessions_are_isolated_by_patient_id() -> None:
    """Cross-patient leakage check — same clinician, different patients."""
    mem = ConversationMemory()
    mem.record(user_id="u1", patient_id="Patient/1", session_id="s",
               turn=_turn("user", "patient one"))
    mem.record(user_id="u1", patient_id="Patient/2", session_id="s",
               turn=_turn("user", "patient two"))
    p1 = mem.turns(user_id="u1", patient_id="Patient/1", session_id="s")
    p2 = mem.turns(user_id="u1", patient_id="Patient/2", session_id="s")
    assert [t.content for t in p1] == ["patient one"]
    assert [t.content for t in p2] == ["patient two"]


def test_ttl_eviction() -> None:
    """A turn older than TTL must be evicted on the next write."""
    mem = ConversationMemory(ttl_seconds=1)
    old = time.time() - 10  # 10 seconds ago
    mem.record(user_id="u1", patient_id="Patient/1", session_id="s",
               turn=_turn("user", "stale", ts=old))
    # Force a write to a different session — eviction runs at the
    # start of every record/turns call.
    mem.record(user_id="u1", patient_id="Patient/1", session_id="other",
               turn=_turn("user", "fresh"))
    # The stale session is gone.
    assert mem.turns(user_id="u1", patient_id="Patient/1", session_id="s") == []


def test_max_sessions_lru_eviction() -> None:
    """Beyond ``max_sessions`` the least-recently-used session is dropped."""
    mem = ConversationMemory(max_sessions=2)
    mem.record(user_id="u1", patient_id="Patient/1", session_id="a",
               turn=_turn("user", "a"))
    mem.record(user_id="u1", patient_id="Patient/1", session_id="b",
               turn=_turn("user", "b"))
    mem.record(user_id="u1", patient_id="Patient/1", session_id="c",
               turn=_turn("user", "c"))
    # 'a' was least recently used; it should be gone.
    assert mem.turns(user_id="u1", patient_id="Patient/1", session_id="a") == []
    # 'b' and 'c' remain.
    assert mem.turns(user_id="u1", patient_id="Patient/1", session_id="b")
    assert mem.turns(user_id="u1", patient_id="Patient/1", session_id="c")


def test_empty_key_field_raises() -> None:
    """Empty user/patient/session ids must raise — never silently merge sessions."""
    mem = ConversationMemory()
    with pytest.raises(ValueError, match="non-empty"):
        mem.record(user_id="", patient_id="Patient/1", session_id="s",
                   turn=_turn("user", "x"))
    with pytest.raises(ValueError, match="non-empty"):
        mem.turns(user_id="u1", patient_id="", session_id="s")


def test_content_is_capped_to_protect_prompt_budget() -> None:
    """Per-turn content must be hard-capped so the prompt budget is bounded."""
    big = "x" * 10_000
    turn = _turn("user", big)
    assert len(turn.content) <= ConversationTurn.MAX_CONTENT_CHARS + 1  # +1 for the ellipsis


def test_invalid_constructor_args() -> None:
    """The constructor must reject zero/negative caps."""
    with pytest.raises(ValueError, match="max_turns_per_session"):
        ConversationMemory(max_turns_per_session=0)
    with pytest.raises(ValueError, match="ttl_seconds"):
        ConversationMemory(ttl_seconds=0)
    with pytest.raises(ValueError, match="max_sessions"):
        ConversationMemory(max_sessions=-1)


def test_render_history_for_prompt_pairs_user_and_assistant() -> None:
    turns = [
        _turn("user", "what's her most recent CRP?", purpose="follow_up_question"),
        _turn("assistant", "answered: highest CRP 42 mg/L"),
        _turn("user", "and uric acid?"),
    ]
    rendered = render_history_for_prompt(turns)
    assert "[prior turn 1: user (follow_up_question)] what's her most recent CRP?" in rendered
    assert "[prior turn 1: assistant] answered: highest CRP 42 mg/L" in rendered
    assert "[prior turn 2: user (follow_up_question)] and uric acid?" in rendered


def test_render_history_for_empty_returns_empty_string() -> None:
    assert render_history_for_prompt([]) == ""
