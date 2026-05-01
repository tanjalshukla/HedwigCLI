from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.autonomy import AutonomyPreferences
from sc.trust_db import TrustDB


class TrustDBAutonomyTests(unittest.TestCase):
    def test_merge_autonomy_preferences_from_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            learned = db.merge_autonomy_preferences(
                repo,
                AutonomyPreferences(
                    prefer_fewer_checkins=True,
                    allowed_checkin_topics=("api", "schema", "security", "signature"),
                    skip_low_risk_plan_checkpoint=True,
                ),
            )
            self.assertGreaterEqual(len(learned), 1)
            prefs = db.autonomy_preferences(repo)
            self.assertTrue(prefs.prefer_fewer_checkins)
            self.assertIn("api", prefs.allowed_checkin_topics)
            self.assertTrue(prefs.skip_low_risk_plan_checkpoint)

    def test_merge_autonomy_preferences_unions_topics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            db.merge_autonomy_preferences(
                repo,
                AutonomyPreferences(
                    prefer_fewer_checkins=True,
                    allowed_checkin_topics=("api",),
                ),
            )
            db.merge_autonomy_preferences(
                repo,
                AutonomyPreferences(
                    allowed_checkin_topics=("signature", "security"),
                ),
            )
            prefs = db.autonomy_preferences(repo)
            self.assertEqual(prefs.allowed_checkin_topics, ("api", "security", "signature"))

    def test_revoke_autonomy_preference_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            db.merge_autonomy_preferences(
                repo,
                AutonomyPreferences(prefer_fewer_checkins=True),
            )
            self.assertTrue(db.autonomy_preferences(repo).prefer_fewer_checkins)
            revoked = db.revoke_autonomy_preference(repo, prefer_fewer_checkins=True)
            self.assertGreater(len(revoked), 0)
            self.assertFalse(db.autonomy_preferences(repo).prefer_fewer_checkins)

    def test_revoke_autonomy_preference_topics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            db.merge_autonomy_preferences(
                repo,
                AutonomyPreferences(allowed_checkin_topics=("api", "schema", "security")),
            )
            db.revoke_autonomy_preference(repo, topics=("api",))
            prefs = db.autonomy_preferences(repo)
            self.assertNotIn("api", prefs.allowed_checkin_topics)
            self.assertIn("schema", prefs.allowed_checkin_topics)
            self.assertIn("security", prefs.allowed_checkin_topics)

    def test_revoke_nothing_matching_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            db.merge_autonomy_preferences(
                repo,
                AutonomyPreferences(prefer_fewer_checkins=True),
            )
            revoked = db.revoke_autonomy_preference(repo, topics=("api",))
            self.assertEqual(revoked, [])
            self.assertTrue(db.autonomy_preferences(repo).prefer_fewer_checkins)

    def test_delete_autonomy_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            db.merge_autonomy_preferences(repo, AutonomyPreferences(prefer_fewer_checkins=True))
            self.assertTrue(db.autonomy_preferences(repo).prefer_fewer_checkins)
            removed = db.delete_autonomy_preferences(repo)
            self.assertEqual(removed, 1)
            self.assertFalse(db.autonomy_preferences(repo).prefer_fewer_checkins)

    def test_verification_failure_rate_and_model_confidence_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="demo/feature.py",
                change_type="general_change",
                diff_size=2,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="proceed",
                policy_score=1.0,
                user_decision="approve",
                verification_passed=True,
                participant_id="p1",
                study_run_id="run-1",
                study_task_id="task-a",
                autonomy_mode="balanced",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s2",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="demo/feature.py",
                change_type="general_change",
                diff_size=2,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=1,
                prior_denials=0,
                policy_action="proceed",
                policy_score=0.8,
                user_decision="approve",
                verification_passed=False,
                participant_id="p1",
                study_run_id="run-1",
                study_task_id="task-a",
                autonomy_mode="balanced",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s3",
                task="task",
                stage="planning",
                action_type="check_in",
                file_path="__session__",
                change_type="decision_point",
                diff_size=None,
                blast_radius=None,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=0.0,
                user_decision="approve",
                model_confidence_self_report=0.25,
                check_in_initiator="model_proactive",
                participant_id="p1",
                study_run_id="run-1",
                study_task_id="task-a",
                autonomy_mode="balanced",
            )

            failure_rate = db.verification_failure_rate(repo, "demo/feature.py")
            self.assertEqual(failure_rate, 0.5)
            confidence = db.model_confidence_stats(repo, file_path="demo/feature.py")
            self.assertEqual(confidence.samples, 1)
            assert confidence.average is not None
            self.assertAlmostEqual(confidence.average, 0.25, places=2)
            self.assertEqual(db.latest_session_id(repo), "s3")
            session_rows = db.session_traces(repo, "s1")
            self.assertEqual(session_rows[0]["participant_id"], "p1")
            self.assertEqual(session_rows[0]["study_run_id"], "run-1")
            self.assertEqual(session_rows[0]["study_task_id"], "task-a")
            self.assertEqual(session_rows[0]["autonomy_mode"], "balanced")


if __name__ == "__main__":
    unittest.main()
