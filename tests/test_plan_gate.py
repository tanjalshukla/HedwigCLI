from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.autonomy import AutonomyPreferences
from sc.plan_gate import decide_plan_checkpoint
from sc.schema import IntentDeclaration
from sc.trust_db import HardConstraint, TrustDB


class PlanGateTests(unittest.TestCase):
    def test_strict_mode_always_requires_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            declaration = IntentDeclaration(
                task_summary="Update docs",
                planned_files=["README.md"],
                planned_actions=["edit_code"],
                planned_commands=[],
            )
            decision = decide_plan_checkpoint(
                trust_db=db,
                repo_root="/tmp/repo",
                declaration=declaration,
                strict=True,
                max_auto_files=2,
            )
            self.assertTrue(decision.required)
            self.assertIn("strict plan gate enabled", decision.reasons)

    def test_low_risk_trusted_single_file_skips_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            for _ in range(3):
                db.record_trace(
                    repo_root=repo,
                    session_id="s1",
                    task="task",
                    stage="apply",
                    action_type="write_request",
                    file_path="docs/readme.md",
                    change_type="documentation",
                    diff_size=2,
                    blast_radius=1,
                    existing_lease=False,
                    lease_type=None,
                    prior_approvals=0,
                    prior_denials=0,
                    policy_action="proceed",
                    policy_score=1.0,
                    user_decision="auto_approve",
                )
            declaration = IntentDeclaration(
                task_summary="Add one doc line",
                planned_files=["docs/readme.md"],
                planned_actions=["edit_code"],
                planned_commands=[],
            )
            decision = decide_plan_checkpoint(
                trust_db=db,
                repo_root=repo,
                declaration=declaration,
                strict=False,
                max_auto_files=1,
            )
            self.assertFalse(decision.required)

    def test_constraints_require_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            db.replace_constraints(
                repo_root=repo,
                source="AGENTS.md",
                constraints=[
                    HardConstraint(
                        path_pattern="src/auth/*",
                        constraint_type="always_check_in",
                        source="AGENTS.md",
                        overridable=False,
                    )
                ],
            )
            declaration = IntentDeclaration(
                task_summary="Auth changes",
                planned_files=["src/auth/login.py"],
                planned_actions=["edit_code"],
                planned_commands=[],
            )
            decision = decide_plan_checkpoint(
                trust_db=db,
                repo_root=repo,
                declaration=declaration,
                strict=False,
                max_auto_files=3,
            )
            self.assertTrue(decision.required)
            self.assertTrue(any("constrained files" in reason for reason in decision.reasons))

    def test_run_tests_does_not_force_checkpoint_for_trusted_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            for _ in range(3):
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
                    user_decision="auto_approve",
                )
            declaration = IntentDeclaration(
                task_summary="Tighten validation and run tests",
                planned_files=["demo/feature.py"],
                planned_actions=["edit_code", "run_tests"],
                planned_commands=["pytest -q"],
            )
            decision = decide_plan_checkpoint(
                trust_db=db,
                repo_root=repo,
                declaration=declaration,
                strict=False,
                max_auto_files=1,
            )
            self.assertFalse(decision.required)

    def test_autonomy_preference_skips_low_risk_multifile_plan_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            declaration = IntentDeclaration(
                task_summary="Low-risk cleanup",
                planned_files=["demo/checkin/service.py", "demo/feature.py"],
                planned_actions=["edit_code", "run_tests"],
                planned_commands=["pytest -q"],
                workflow_phase="planning",
            )
            decision = decide_plan_checkpoint(
                trust_db=db,
                repo_root="/tmp/repo",
                declaration=declaration,
                strict=False,
                max_auto_files=1,
                autonomy_preferences=AutonomyPreferences(
                    prefer_fewer_checkins=True,
                    skip_low_risk_plan_checkpoint=True,
                ),
            )
            self.assertFalse(decision.required)

    def test_high_blast_radius_requires_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            target = repo_path / "core.py"
            target.write_text("def f():\n    return 1\n")
            for idx in range(6):
                consumer = repo_path / f"use_{idx}.py"
                consumer.write_text("from core import f\n")

            db = TrustDB(repo_path / ".sc_trust.db")
            declaration = IntentDeclaration(
                task_summary="Change core implementation",
                planned_files=["core.py"],
                planned_actions=["edit_code"],
                planned_commands=[],
            )
            decision = decide_plan_checkpoint(
                trust_db=db,
                repo_root=str(repo_path),
                declaration=declaration,
                strict=False,
                max_auto_files=1,
                repo_root_path=repo_path,
            )
            self.assertTrue(decision.required)
            self.assertTrue(any("high import count" in reason for reason in decision.reasons))

    def test_spec_without_requirement_mapping_requires_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            declaration = IntentDeclaration(
                task_summary="Implement feature from spec",
                planned_files=["demo/feature.py"],
                planned_actions=["edit_code"],
                planned_commands=[],
                expected_change_types=["general_change"],
            )
            decision = decide_plan_checkpoint(
                trust_db=db,
                repo_root="/tmp/repo",
                declaration=declaration,
                strict=False,
                max_auto_files=1,
                spec_required=True,
            )
            self.assertTrue(decision.required)
            self.assertTrue(any("spec provided" in reason for reason in decision.reasons))

    def test_potential_deviations_alone_do_not_force_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            declaration = IntentDeclaration(
                task_summary="Implement feature from spec",
                planned_files=["demo/feature.py"],
                planned_actions=["edit_code"],
                planned_commands=[],
                expected_change_types=["general_change"],
                requirements_covered=["Req-1"],
                potential_deviations=["Minor naming nit, won't affect callers."],
            )
            decision = decide_plan_checkpoint(
                trust_db=db,
                repo_root="/tmp/repo",
                declaration=declaration,
                strict=False,
                max_auto_files=1,
                spec_required=True,
            )
            self.assertFalse(decision.required)


if __name__ == "__main__":
    unittest.main()
