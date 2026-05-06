"""In-process conversation memory for the multi-turn agent.

Why this exists. ``USERS.md`` §3 makes mid-visit follow-ups (Use Case
C) and within-the-room clarifications part of the agent's job. The
sidecar's first cut at ``/chat`` was stateless — every call ran the
canonical pairwise procedure against a fresh snapshot — so a follow-up
like "what about her CRP?" had no way to inherit the symptom or
candidate the prior turn was reasoning about. This module adds the
smallest viable session memory: a process-local store keyed by
``(user_id, patient_id, session_id)`` that retains the last few
``ConversationTurn`` records.

What this is **not**:

* It is not a long-term memory. The store is in-process and bounded.
  Sessions older than ``DEFAULT_TTL_SECONDS`` get evicted on the next
  ``record`` call.
* It is not a PHI store. Each turn keeps the clinician's question
  text and a redacted summary of the agent's verdict. Free-text
  diagnoses, lab values, or chart quotes never go in.
* It is not the audit log. The audit log is hash-chained, persisted,
  and optimised for compliance review. The conversation memory is a
  short-lived working buffer for the model's next prompt.

Two failure modes the design protects against:

* Cross-clinician leakage. The composite key includes ``user_id`` so
  one clinician's session cannot resurrect another clinician's prior
  message even if the patient_id matches.
* Stale-cache identity drift. ``Settings.session_memory_max_turns``
  caps the per-session history; older turns are dropped FIFO so a
  long-running session never grows unbounded.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable

# Per-session ring-buffer cap. The model prompt only carries the last
# few turns anyway; keeping more wastes memory.
DEFAULT_MAX_TURNS: int = 8

# Per-session inactivity TTL. After this long with no new turn, the
# session is evicted on the next write to keep memory bounded.
DEFAULT_TTL_SECONDS: int = 60 * 60  # 1 hour


@dataclass(frozen=True)
class ConversationTurn:
    """One past turn in a session.

    Fields:

    * ``role`` — "user" for the clinician's message, "assistant" for
      the redacted verdict the agent returned.
    * ``content`` — the verbatim message text for user turns; for
      assistant turns, a short redacted summary (no PHI, no chart
      quotes). The redaction is the caller's responsibility — this
      class enforces a length cap as a final guardrail.
    * ``ts_unix`` — Unix epoch seconds the turn was recorded.
    * ``purpose`` — the ``ChatRequest.purpose`` of the turn so the
      next-turn prompt can show the model whether the prior step was a
      cross-check, an error scan, or another follow-up.
    """

    role: str
    content: str
    ts_unix: float
    purpose: str

    MAX_CONTENT_CHARS: int = field(default=2_000, init=False, repr=False)

    def __post_init__(self) -> None:
        # Hard cap to keep the prompt budget predictable. We chose
        # frozen=True so __setattr__ is blocked; use object.__setattr__
        # to apply the trim once at construction.
        if len(self.content) > self.MAX_CONTENT_CHARS:
            object.__setattr__(
                self, "content", self.content[: self.MAX_CONTENT_CHARS] + "…"
            )


class ConversationMemory:
    """Thread-safe in-process store of recent conversation turns.

    The store is an ``OrderedDict`` so eviction is O(1). The lock is a
    plain ``threading.Lock`` because all operations are short and
    synchronous — there is no network IO under the lock.

    Use :func:`get_default_memory` from the chat handler. Tests
    construct fresh instances directly so they don't share state.
    """

    def __init__(
        self,
        *,
        max_turns_per_session: int = DEFAULT_MAX_TURNS,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_sessions: int = 1024,
    ) -> None:
        if max_turns_per_session <= 0:
            raise ValueError(
                f"max_turns_per_session must be > 0, got {max_turns_per_session}"
            )
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds}")
        if max_sessions <= 0:
            raise ValueError(f"max_sessions must be > 0, got {max_sessions}")
        self._max_turns = max_turns_per_session
        self._ttl = ttl_seconds
        self._max_sessions = max_sessions
        self._store: "OrderedDict[tuple[str, str, str], list[ConversationTurn]]" = (
            OrderedDict()
        )
        self._last_touched: dict[tuple[str, str, str], float] = {}
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────

    def turns(
        self, *, user_id: str, patient_id: str, session_id: str
    ) -> list[ConversationTurn]:
        """Return the recorded turns for this session, oldest first."""
        key = self._key(user_id, patient_id, session_id)
        with self._lock:
            self._evict_stale_locked()
            return list(self._store.get(key, ()))

    def record(
        self,
        *,
        user_id: str,
        patient_id: str,
        session_id: str,
        turn: ConversationTurn,
    ) -> None:
        """Append a turn and trim/evict as needed."""
        key = self._key(user_id, patient_id, session_id)
        with self._lock:
            self._evict_stale_locked()
            history = self._store.get(key)
            if history is None:
                history = []
                self._store[key] = history
            history.append(turn)
            # FIFO trim per session.
            if len(history) > self._max_turns:
                del history[: len(history) - self._max_turns]
            self._last_touched[key] = turn.ts_unix
            # Move-to-end so the LRU eviction below picks the truly
            # oldest session, not the alphabetically first one.
            self._store.move_to_end(key)
            # Bound total session count.
            while len(self._store) > self._max_sessions:
                victim_key, _ = self._store.popitem(last=False)
                self._last_touched.pop(victim_key, None)

    def clear(self) -> None:
        """Drop everything (test helper, not for production)."""
        with self._lock:
            self._store.clear()
            self._last_touched.clear()

    # ── Internal ──────────────────────────────────────────────────

    @staticmethod
    def _key(user_id: str, patient_id: str, session_id: str) -> tuple[str, str, str]:
        # All three are required; an empty value is a programming
        # error (the chat handler validates upstream). Failing here
        # would mean a bug got past validation.
        if not user_id or not patient_id or not session_id:
            raise ValueError(
                "ConversationMemory key requires non-empty user_id, "
                f"patient_id, session_id; got {user_id!r}, {patient_id!r}, "
                f"{session_id!r}"
            )
        return (user_id, patient_id, session_id)

    def _evict_stale_locked(self) -> None:
        # Caller already holds the lock.
        now = time.time()
        cutoff = now - self._ttl
        # Walk the least-recently-used end of the OrderedDict.
        # Anything older than the cutoff goes; stop on the first
        # fresh entry (ordering is by insertion+touch).
        stale: list[tuple[str, str, str]] = []
        for key in self._store:
            if self._last_touched.get(key, 0.0) < cutoff:
                stale.append(key)
            else:
                break
        for key in stale:
            self._store.pop(key, None)
            self._last_touched.pop(key, None)


# Process-wide singleton. The chat handler picks this up via
# ``get_default_memory()`` so tests can swap a fresh instance with
# ``set_default_memory(...)`` instead of monkey-patching the module.

_DEFAULT_MEMORY: ConversationMemory | None = None
_DEFAULT_MEMORY_LOCK = threading.Lock()


def get_default_memory() -> ConversationMemory:
    """Return the process-wide ``ConversationMemory`` singleton."""
    global _DEFAULT_MEMORY
    if _DEFAULT_MEMORY is None:
        with _DEFAULT_MEMORY_LOCK:
            if _DEFAULT_MEMORY is None:
                _DEFAULT_MEMORY = ConversationMemory()
    return _DEFAULT_MEMORY


def set_default_memory(memory: ConversationMemory | None) -> None:
    """Replace the process-wide singleton (test/dev only)."""
    global _DEFAULT_MEMORY
    with _DEFAULT_MEMORY_LOCK:
        _DEFAULT_MEMORY = memory


def render_history_for_prompt(turns: Iterable[ConversationTurn]) -> str:
    """Render a list of turns into a compact prompt prefix.

    Format:

    ```
    [prior turn 1: user ({purpose})] {content}
    [prior turn 1: assistant] {content}
    [prior turn 2: user ({purpose})] {content}
    ...
    ```

    Returns an empty string if there are no turns. Used by
    ``follow_up.py``; ``run_graph`` does not currently see the prior
    turns directly because the pairwise comparator already runs over
    the full snapshot.
    """
    lines: list[str] = []
    pair_index = 0
    for turn in turns:
        if turn.role == "user":
            pair_index += 1
            lines.append(
                f"[prior turn {pair_index}: user ({turn.purpose})] {turn.content}"
            )
        else:
            lines.append(f"[prior turn {pair_index}: assistant] {turn.content}")
    return "\n".join(lines)
