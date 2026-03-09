from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.trust_db import HardConstraint, TrustDB


class TrustDBConstraintTests(unittest.TestCase):
    def test_replace_and_match_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.db"
            db = TrustDB(db_path)
            repo = "/tmp/repo"
            inserted = db.replace_constraints(
                repo_root=repo,
                source="AGENTS.md",
                constraints=[
                    HardConstraint(
                        path_pattern="src/auth/*",
                        constraint_type="always_check_in",
                        source="AGENTS.md",
                        overridable=False,
                    ),
                    HardConstraint(
                        path_pattern="src/auth/token.py",
                        constraint_type="always_deny",
                        source="AGENTS.md",
                        overridable=False,
                    ),
                ],
            )
            self.assertEqual(inserted, 2)

            all_rows = db.list_constraints(repo)
            self.assertEqual(len(all_rows), 2)

            strongest = db.strongest_constraint(repo, "src/auth/token.py")
            self.assertIsNotNone(strongest)
            assert strongest is not None
            self.assertEqual(strongest.constraint_type, "always_deny")

            matched = db.matching_constraints(repo, "src/auth/user.py")
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].constraint_type, "always_check_in")

    def test_split_read_write_constraint_picks_policy_by_access_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.db"
            db = TrustDB(db_path)
            repo = "/tmp/repo"
            db.replace_constraints(
                repo_root=repo,
                source="AGENTS.md",
                constraints=[
                    HardConstraint(
                        path_pattern="src/frontend/*",
                        source="AGENTS.md",
                        overridable=False,
                        read_policy="always_allow",
                        write_policy="always_check_in",
                    )
                ],
            )
            read_constraint = db.strongest_constraint(repo, "src/frontend/app.css", access_type="read")
            write_constraint = db.strongest_constraint(repo, "src/frontend/app.css", access_type="write")
            assert read_constraint is not None
            assert write_constraint is not None
            self.assertEqual(read_constraint.policy_for("read"), "always_allow")
            self.assertEqual(write_constraint.policy_for("write"), "always_check_in")

    def test_add_constraints_deduplicates_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.db"
            db = TrustDB(db_path)
            repo = "/tmp/repo"
            row = HardConstraint.for_both(
                path_pattern="config/prod/*",
                constraint_type="always_deny",
                source="manual_rule",
                overridable=False,
            )
            inserted = db.add_constraints(repo, [row])
            inserted_again = db.add_constraints(repo, [row])
            self.assertEqual(inserted, 1)
            self.assertEqual(inserted_again, 0)
            rows = db.list_constraints(repo)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].path_pattern, "config/prod/*")

    def test_replace_and_list_behavioral_guidelines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.db"
            db = TrustDB(db_path)
            repo = "/tmp/repo"
            inserted = db.replace_behavioral_guidelines(
                repo_root=repo,
                source="AGENTS.md",
                guidelines=[
                    "Always run tests after editing",
                    "Use project logger instead of print",
                    "Always run tests after editing",
                ],
            )
            self.assertEqual(inserted, 2)
            rows = db.list_behavioral_guidelines(repo)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0].source, "AGENTS.md")

            removed = db.delete_behavioral_guidelines(repo, source="AGENTS.md")
            self.assertEqual(removed, 2)
            self.assertEqual(db.list_behavioral_guidelines(repo), [])

    def test_checkin_calibration_grouped_by_initiator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.db"
            db = TrustDB(db_path)
            repo = "/tmp/repo"
            db.record_trace(
                repo_root=repo,
                session_id="s1",
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
                response_time_ms=1200,
                check_in_initiator="model_proactive",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s2",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="src/x.py",
                change_type="logic",
                diff_size=2,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=1,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.3,
                user_decision="deny",
                response_time_ms=2500,
                check_in_initiator="policy",
            )

            stats = db.checkin_calibration(repo)
            keyed = {(row.initiator, row.stage): row for row in stats}
            self.assertIn(("model_proactive", "planning"), keyed)
            self.assertIn(("policy", "apply"), keyed)
            self.assertEqual(keyed[("model_proactive", "planning")].approvals, 1)
            self.assertEqual(keyed[("policy", "apply")].denials, 1)


if __name__ == "__main__":
    unittest.main()
