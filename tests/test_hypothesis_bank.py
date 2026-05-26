from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sc.hypothesis_bank import (
    PRUNE_THRESHOLD,
    SURFACE_CONFIDENCE,
    MIN_EVIDENCE,
    _evidence_for_trace,
    _candidate_confidence,
    seed_candidates_from_session,
    update_evidence,
    get_ready_hypothesis,
    mark_candidate_surfaced,
)
from sc.preference_inference import SessionSummary
from sc.preferences import PushbackType, UserPersona
from sc.trust_db import TrustDB


def _make_db() -> tuple[TrustDB, str, str]:
    tmp = tempfile.mkdtemp()
    db = TrustDB(Path(tmp) / "trust.db")
    return db, "/tmp/repo", "session-1"


def _summary(n_turns: int = 10, n_failures: int = 0) -> SessionSummary:
    return SessionSummary(
        session_id="s1",
        n_turns=n_turns,
        n_approvals=n_turns,
        n_denials=0,
        n_feedback=0,
        n_failures=n_failures,
        mean_edit_distance=0.05,
        mean_review_seconds=5.0,
        distinct_tasks=1,
        n_interruptions=0,
        n_auto_approvals=0,
    )


def _trace(pushback: str = "", decision: str = "auto_approve",
           blast: int = 1, rubber: bool = False,
           response_ms: int = 3000, verif: int | None = None) -> dict:
    return {
        "pushback_type": pushback,
        "user_decision": decision,
        "blast_radius": blast,
        "rubber_stamp": 1 if rubber else 0,
        "response_time_ms": response_ms,
        "verification_passed": verif,
    }


class EvidenceScorerTests(unittest.TestCase):
    def test_scope_constraint_trace_adds_for(self) -> None:
        f, a = _evidence_for_trace("scope_constraint", _trace(pushback=PushbackType.SCOPE_CONSTRAINT.value))
        self.assertEqual(f, 1)
        self.assertEqual(a, 0)

    def test_multi_file_auto_approve_adds_against_scope(self) -> None:
        f, a = _evidence_for_trace("scope_constraint", _trace(blast=3))
        self.assertEqual(f, 0)
        self.assertEqual(a, 1)

    def test_failure_report_adds_for_failure_reactive(self) -> None:
        f, a = _evidence_for_trace("failure_reactive", _trace(pushback=PushbackType.FAILURE_REPORT.value))
        self.assertEqual(f, 1)
        self.assertEqual(a, 0)

    def test_verif_pass_adds_against_failure_reactive(self) -> None:
        f, a = _evidence_for_trace("failure_reactive", _trace(verif=1))
        self.assertEqual(f, 0)
        self.assertEqual(a, 1)

    def test_rubber_stamp_fast_adds_for_rapid_approver(self) -> None:
        f, a = _evidence_for_trace("rapid_approver", _trace(rubber=True, response_ms=1000))
        self.assertEqual(f, 1)
        self.assertEqual(a, 0)

    def test_slow_review_adds_against_rapid(self) -> None:
        f, a = _evidence_for_trace("rapid_approver", _trace(response_ms=10000))
        self.assertEqual(f, 0)
        self.assertEqual(a, 1)


class CandidateConfidenceTests(unittest.TestCase):
    def test_pure_for(self) -> None:
        self.assertEqual(_candidate_confidence(5, 0), 1.0)

    def test_pure_against(self) -> None:
        self.assertEqual(_candidate_confidence(0, 5), 0.0)

    def test_even_split(self) -> None:
        self.assertAlmostEqual(_candidate_confidence(3, 3), 0.5)

    def test_empty(self) -> None:
        self.assertAlmostEqual(_candidate_confidence(0, 0), 0.5)


class BankIntegrationTests(unittest.TestCase):
    def test_seed_adds_candidate_when_threshold_met(self) -> None:
        db, repo, session = _make_db()
        pushbacks = {PushbackType.SCOPE_CONSTRAINT.value: 3}
        new_ids = seed_candidates_from_session(
            trust_db=db,
            repo_root=repo,
            session_id=session,
            session_summary=_summary(),
            pushback_counts=pushbacks,
        )
        self.assertEqual(len(new_ids), 1)

    def test_seed_deduplicates_same_driver(self) -> None:
        db, repo, session = _make_db()
        pushbacks = {PushbackType.SCOPE_CONSTRAINT.value: 3}
        seed_candidates_from_session(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), pushback_counts=pushbacks,
        )
        ids2 = seed_candidates_from_session(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), pushback_counts=pushbacks,
        )
        self.assertEqual(len(ids2), 0)

    def test_evidence_accumulation_promotes_candidate(self) -> None:
        db, repo, session = _make_db()
        pushbacks = {PushbackType.SCOPE_CONSTRAINT.value: 3}
        seed_candidates_from_session(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), pushback_counts=pushbacks,
        )
        # Feed supporting traces until confidence threshold is met.
        for _ in range(MIN_EVIDENCE):
            update_evidence(
                trust_db=db, repo_root=repo, session_id=session,
                trace=_trace(pushback=PushbackType.SCOPE_CONSTRAINT.value),
            )
        hyp = get_ready_hypothesis(trust_db=db, repo_root=repo, session_id=session)
        self.assertIsNotNone(hyp)
        assert hyp is not None
        self.assertEqual(hyp.driver, "scope_constraint")
        self.assertIn("confidence", hyp.rationale)

    def test_contradicting_evidence_prunes_candidate(self) -> None:
        db, repo, session = _make_db()
        pushbacks = {PushbackType.SCOPE_CONSTRAINT.value: 3}
        seed_candidates_from_session(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), pushback_counts=pushbacks,
        )
        # Feed mostly contradicting traces.
        for _ in range(MIN_EVIDENCE):
            update_evidence(
                trust_db=db, repo_root=repo, session_id=session,
                trace=_trace(blast=3),  # multi-file auto-approve = against scope_constraint
            )
        hyp = get_ready_hypothesis(trust_db=db, repo_root=repo, session_id=session)
        self.assertIsNone(hyp)

    def test_mark_surfaced_sets_status(self) -> None:
        db, repo, session = _make_db()
        pushbacks = {PushbackType.SCOPE_CONSTRAINT.value: 3}
        seed_candidates_from_session(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), pushback_counts=pushbacks,
        )
        for _ in range(MIN_EVIDENCE):
            update_evidence(
                trust_db=db, repo_root=repo, session_id=session,
                trace=_trace(pushback=PushbackType.SCOPE_CONSTRAINT.value),
            )
        hyp = get_ready_hypothesis(trust_db=db, repo_root=repo, session_id=session)
        assert hyp is not None
        mark_candidate_surfaced(
            trust_db=db, repo_root=repo, session_id=session,
            driver=hyp.driver, confirmed=True,
        )
        # Should not be returned again.
        hyp2 = get_ready_hypothesis(trust_db=db, repo_root=repo, session_id=session)
        self.assertIsNone(hyp2)

    def test_delegating_persona_still_seeds_candidates(self) -> None:
        # Delegating sessions still seed — evidence accumulates.
        # The surfacing gate is in apply_stage, not here.
        db, repo, session = _make_db()
        ids = seed_candidates_from_session(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(),
            pushback_counts={PushbackType.SCOPE_CONSTRAINT.value: 5},
            inferred_persona=UserPersona.DELEGATING,
        )
        self.assertGreater(len(ids), 0)

    def test_surface_confidence_threshold_boundary(self) -> None:
        """update_evidence promotes a candidate to ready_to_surface exactly when
        evidence_for / total >= SURFACE_CONFIDENCE after MIN_EVIDENCE traces.

        This is the gate that Scene 2 of the demo depends on: the panel fires
        only after the fourth supporting trace, not before.
        """
        db, repo, session = _make_db()
        pushbacks = {PushbackType.SCOPE_CONSTRAINT.value: 3}
        seed_candidates_from_session(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), pushback_counts=pushbacks,
        )

        # Feed (MIN_EVIDENCE - 1) supporting traces — confidence will be high
        # but total < MIN_EVIDENCE, so the candidate must NOT surface yet.
        for _ in range(MIN_EVIDENCE - 1):
            update_evidence(
                trust_db=db, repo_root=repo, session_id=session,
                trace=_trace(pushback=PushbackType.SCOPE_CONSTRAINT.value),
            )
        # Should not be ready yet — total evidence count is below the gate.
        hyp_early = get_ready_hypothesis(trust_db=db, repo_root=repo, session_id=session)
        self.assertIsNone(
            hyp_early,
            f"Hypothesis surfaced too early (before MIN_EVIDENCE={MIN_EVIDENCE} traces)",
        )

        # One more supporting trace pushes total to MIN_EVIDENCE with confidence = 1.0
        # which is >= SURFACE_CONFIDENCE; now it must surface.
        ready_ids = update_evidence(
            trust_db=db, repo_root=repo, session_id=session,
            trace=_trace(pushback=PushbackType.SCOPE_CONSTRAINT.value),
        )
        self.assertGreater(len(ready_ids), 0, "Expected candidate promoted to ready_to_surface")
        hyp = get_ready_hypothesis(trust_db=db, repo_root=repo, session_id=session)
        self.assertIsNotNone(hyp)
        assert hyp is not None
        # Rationale must embed the confidence figure visible in the demo panel.
        self.assertIn("confidence", hyp.rationale)
        self.assertIn(f"{MIN_EVIDENCE}/{MIN_EVIDENCE}", hyp.rationale)

    def test_below_prune_threshold_marks_rejected_not_deleted(self) -> None:
        """Contradicting evidence below PRUNE_THRESHOLD stores 'rejected', not deletes.

        TICL-inspired: negative examples are preserved so the /prefs display
        can show 'I considered this but evidence didn't support it.'
        """
        import sqlite3
        db, repo, session = _make_db()
        pushbacks = {PushbackType.SCOPE_CONSTRAINT.value: 3}
        seed_candidates_from_session(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), pushback_counts=pushbacks,
        )
        # Feed all-against traces.
        for _ in range(MIN_EVIDENCE):
            update_evidence(
                trust_db=db, repo_root=repo, session_id=session,
                trace=_trace(blast=3),  # multi-file auto-approve = against scope_constraint
            )
        # Row must still exist in DB, status='rejected'.
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT status FROM hypothesis_candidates WHERE repo_root = ? AND session_id = ?",
                (repo, session),
            ).fetchall()
        self.assertEqual(len(rows), 1, "Row should not be deleted")
        self.assertEqual(rows[0]["status"], "rejected")


class FakeNoticerClient:
    """Stand-in for AgentClient — returns canned JSON from `_call`."""
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def _call(self, session, max_tokens, temperature):
        self.calls += 1
        return self.payload


class NoticerTests(unittest.TestCase):
    def _seed_traces(self, db: TrustDB, repo: str, session: str, n: int) -> list[int]:
        ids: list[int] = []
        for i in range(n):
            db.record_trace(
                repo_root=repo,
                session_id=session,
                task="task",
                stage="apply",
                action_type="write_request",
                file_path=f"a{i}.py",
                change_type="logic",
                diff_size=1,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="proceed",
                policy_score=1.0,
                user_decision="approve",
            )
        rows = db.session_traces(repo, session)
        return [int(r["id"]) for r in rows]

    def test_noticer_drops_uncited_candidates(self):
        from sc.hypothesis_bank import maybe_generate_llm_hypotheses
        db, repo, session = _make_db()
        self._seed_traces(db, repo, session, 5)
        client = FakeNoticerClient(json.dumps([
            {"driver": "uncited", "prompt": "P?", "rationale": "R", "evidence_trace_ids": []}
        ]))
        new = maybe_generate_llm_hypotheses(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), turn_count=10, client=client,
        )
        self.assertEqual(new, [])

    def test_noticer_drops_hallucinated_citations(self):
        from sc.hypothesis_bank import maybe_generate_llm_hypotheses
        db, repo, session = _make_db()
        self._seed_traces(db, repo, session, 3)
        client = FakeNoticerClient(json.dumps([
            {"driver": "ghost", "prompt": "P?", "rationale": "R",
             "evidence_trace_ids": [99999]}
        ]))
        new = maybe_generate_llm_hypotheses(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), turn_count=10, client=client,
        )
        self.assertEqual(new, [])

    def test_noticer_seeds_evidence_and_surfaces_immediately(self):
        from sc.hypothesis_bank import maybe_generate_llm_hypotheses
        db, repo, session = _make_db()
        ids = self._seed_traces(db, repo, session, 5)
        client = FakeNoticerClient(json.dumps([
            {"driver": "novel_pattern", "prompt": "P?",
             "rationale": "grounded in cited traces",
             "evidence_trace_ids": ids[:3]}
        ]))
        new = maybe_generate_llm_hypotheses(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), turn_count=10, client=client,
        )
        self.assertEqual(len(new), 1)
        with db._connect() as conn:
            row = conn.execute(
                "SELECT status, source, evidence_for FROM hypothesis_candidates WHERE id = ?",
                (new[0],),
            ).fetchone()
        self.assertEqual(row["source"], "llm_generated")
        self.assertEqual(row["status"], "ready_to_surface")
        self.assertGreaterEqual(int(row["evidence_for"]), MIN_EVIDENCE)

    def test_noticer_skips_when_driver_already_pending(self):
        from sc.hypothesis_bank import maybe_generate_llm_hypotheses
        db, repo, session = _make_db()
        ids = self._seed_traces(db, repo, session, 5)
        # Pre-seed a candidate with the driver the LLM will propose.
        db.add_hypothesis_candidate(
            repo_root=repo, session_id=session, driver="dup_driver",
            source="rule_based", prompt="existing", rationale="r",
            preference_json="{}",
        )
        client = FakeNoticerClient(json.dumps([
            {"driver": "dup_driver", "prompt": "P?", "rationale": "R",
             "evidence_trace_ids": ids[:2]}
        ]))
        new = maybe_generate_llm_hypotheses(
            trust_db=db, repo_root=repo, session_id=session,
            session_summary=_summary(), turn_count=10, client=client,
        )
        self.assertEqual(new, [])


if __name__ == "__main__":
    unittest.main()
