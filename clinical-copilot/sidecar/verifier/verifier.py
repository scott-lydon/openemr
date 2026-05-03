"""The verifier itself.

Two stages, both deterministic:

1. **Source attribution.** Every claim in the aggregated result must cite a
   row whose ``(table, row_id)`` exists in the snapshot. Claims without a
   resolvable citation are stripped.
2. **Rule check.** Run the curated rule store against the snapshot. If any
   rule fires ``block`` we return ``BLOCKED`` and the planner re-plans.
   ``warn`` rules annotate the response inline.

The verifier is **not** the place where "is this pair inconsistent"
reasoning happens. That is the LLM pair judge (ARCHITECTURE.md §4.1).
The verifier is the deterministic guard that runs after.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sidecar._compat import StrEnum
from sidecar.snapshot import PatientSnapshot

from .rules import RuleAction, RuleHit, RuleStore


class VerifierVerdict(StrEnum):
    PASSED = "passed"
    WARNED = "warned"
    BLOCKED = "blocked"


@dataclass
class VerificationOutcome:
    """The full verifier output."""

    verdict: VerifierVerdict
    stripped_candidate_labels: list[str] = field(default_factory=list)
    stripped_flag_labels: list[str] = field(default_factory=list)
    rule_hits: list[RuleHit] = field(default_factory=list)
    blocking_rule_ids: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)


class Verifier:
    """Two-stage deterministic guard."""

    def __init__(self, rule_store: RuleStore) -> None:
        self.rule_store = rule_store

    # ─── Source attribution stage ──────────────────────────────────────

    @staticmethod
    def _snapshot_index(snapshot: PatientSnapshot) -> set[tuple[str, str]]:
        """Build the set of ``(table, str(row_id))`` pairs in the snapshot.

        A claim is well-cited if its evidence row resolves to one of these.
        """
        idx: set[tuple[str, str]] = set()
        for p in snapshot.active_problems:
            idx.add((p.provenance.table, str(p.provenance.row_id)))
        for m in snapshot.medications:
            idx.add((m.provenance.table, str(m.provenance.row_id)))
        for a in snapshot.allergies:
            idx.add((a.provenance.table, str(a.provenance.row_id)))
        for v in snapshot.recent_vitals:
            idx.add((v.provenance.table, str(v.provenance.row_id)))
        for lab in snapshot.recent_labs:
            idx.add((lab.provenance.table, str(lab.provenance.row_id)))
        return idx

    @staticmethod
    def _candidate_attribution_ok(
        candidate_provenance_table: str,
        candidate_provenance_row_id: object,
        snapshot_index: set[tuple[str, str]],
    ) -> bool:
        return (candidate_provenance_table, str(candidate_provenance_row_id)) in snapshot_index

    # ─── Rule stage ────────────────────────────────────────────────────

    def _evaluate_rules(self, snapshot: PatientSnapshot) -> tuple[VerifierVerdict, list[RuleHit], list[str]]:
        hits = self.rule_store.evaluate_against_snapshot(snapshot)
        blocking_ids: list[str] = []
        verdict = VerifierVerdict.PASSED
        for h in hits:
            if h.rule.action is RuleAction.BLOCK:
                verdict = VerifierVerdict.BLOCKED
                blocking_ids.append(h.rule.id)
            elif h.rule.action is RuleAction.WARN and verdict is VerifierVerdict.PASSED:
                verdict = VerifierVerdict.WARNED
        return verdict, hits, blocking_ids

    # ─── Data-gap reporting ────────────────────────────────────────────

    @staticmethod
    def _data_gaps(snapshot: PatientSnapshot) -> list[str]:
        """Surface the 1-2 most clinically actionable gaps.

        ARCHITECTURE.md §10 ("How do we present 'data gaps' without
        becoming the next ignored alert? Inline annotation, capped at the
        1 or 2 most clinically actionable gaps per response.").

        Each gap message is built from the patient's actual snapshot so
        the gap text is dynamic (it cites the matching active problem
        label and the actual lab window) rather than a static string.
        """
        gaps: list[str] = []

        def _matching_problem(needles: tuple[str, ...]) -> str | None:
            for p in snapshot.active_problems:
                lb = p.label.lower()
                if any(n in lb for n in needles):
                    return p.label
            return None

        def _has_lab(needles: tuple[str, ...]) -> bool:
            return any(
                any(n in lab.label.lower() for n in needles)
                for lab in snapshot.recent_labs
            )

        def _lab_window_phrase() -> str:
            # Cite the most recent observation we *do* have so the doctor
            # can judge how stale the labs section is. Falls back to a
            # generic phrase if the patient has zero labs on file.
            if not snapshot.recent_labs:
                return "in the recent labs window (no labs on file)"
            latest = max(
                (lab.observed_at for lab in snapshot.recent_labs if lab.observed_at),
                default=None,
            )
            if latest is None:
                return "in the recent labs window"
            return f"in the labs window through {latest.date().isoformat()}"

        # Gout but no recent uric acid → the gout-vs-infection
        # differentiator from the Stage 0 narrative.
        gout_problem = _matching_problem(("gout",))
        if gout_problem and not _has_lab(("urate", "uric acid")):
            gaps.append(
                f"no recent uric acid measured for active problem "
                f"“{gout_problem}” — would resolve gout vs infection"
            )

        # Diabetes but no HbA1c in the lab window.
        dm_problem = _matching_problem(("diabetes",))
        if dm_problem and not _has_lab(("hba1c", "a1c", "hemoglobin a1c")):
            gaps.append(
                f"no HbA1c measured for active problem "
                f"“{dm_problem}” {_lab_window_phrase()}"
            )

        return gaps[:2]

    # ─── Public API ────────────────────────────────────────────────────

    def verify(
        self,
        snapshot: PatientSnapshot,
        *,
        candidate_provenance: list[tuple[str, str, str]] | None = None,
        flag_provenance: list[tuple[str, str, str, str, str]] | None = None,
    ) -> VerificationOutcome:
        """Run both stages.

        ``candidate_provenance`` is a list of ``(label, table, row_id)`` for
        Use Case A candidates; ``flag_provenance`` is a list of
        ``(label_a, table_a, row_id_a, label_b, table_b)`` … only the
        labels and provenance are needed at this stage.
        """
        index = self._snapshot_index(snapshot)
        stripped_candidates: list[str] = []
        if candidate_provenance:
            for label, table, row_id in candidate_provenance:
                if not self._candidate_attribution_ok(table, row_id, index):
                    stripped_candidates.append(label)

        stripped_flags: list[str] = []
        if flag_provenance:
            for la, ta, ra, lb, tb in flag_provenance:  # noqa: PLW2901
                a_ok = (ta, str(ra)) in index
                b_ok = (tb, str(la)) in index or (tb, str(lb)) in index  # tolerant
                if not (a_ok and b_ok):
                    stripped_flags.append(f"{la} × {lb}")

        verdict, hits, blocking = self._evaluate_rules(snapshot)
        annotations = [
            f"[{h.rule.id}] {h.rule.description.strip()}"
            for h in hits
            if h.rule.action is RuleAction.WARN
        ]
        gaps = self._data_gaps(snapshot)

        return VerificationOutcome(
            verdict=verdict,
            stripped_candidate_labels=stripped_candidates,
            stripped_flag_labels=stripped_flags,
            rule_hits=hits,
            blocking_rule_ids=blocking,
            annotations=annotations,
            data_gaps=gaps,
        )
