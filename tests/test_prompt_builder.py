from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.prompt_builder import build_run_system_prompt
from sc.trust_db import HardConstraint, TrustDB


class PromptBuilderTests(unittest.TestCase):
    def test_build_prompt_includes_dynamic_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.db"
            db = TrustDB(db_path)
            repo = "/tmp/repo"

            db.replace_constraints(
                repo_root=repo,
                source="DEMO_RULES.md",
                constraints=[
                    HardConstraint(
                        path_pattern="src/auth/*",
                        constraint_type="always_check_in",
                        source="DEMO_RULES.md",
                        overridable=False,
                    )
                ],
            )
            db.replace_behavioral_guidelines(
                repo_root=repo,
                source="DEMO_RULES.md",
                guidelines=["Always run tests after editing."],
            )

            for _ in range(3):
                db.record_trace(
                    repo_root=repo,
                    session_id="s1",
                    task="task",
                    stage="apply",
                    action_type="write_request",
                    file_path="src/core/service.py",
                    change_type="logic",
                    diff_size=3,
                    blast_radius=1,
                    existing_lease=False,
                    lease_type=None,
                    prior_approvals=0,
                    prior_denials=0,
                    policy_action="proceed",
                    policy_score=1.0,
                    user_decision="auto_approve",
                )
            for _ in range(2):
                db.record_trace(
                    repo_root=repo,
                    session_id="s2",
                    task="task",
                    stage="apply",
                    action_type="write_request",
                    file_path="src/auth/guard.py",
                    change_type="security",
                    diff_size=2,
                    blast_radius=1,
                    existing_lease=False,
                    lease_type=None,
                    prior_approvals=0,
                    prior_denials=0,
                    policy_action="check_in",
                    policy_score=-1.0,
                    user_decision="deny",
                    user_feedback_text="Use existing auth adapter instead of adding a new dependency.",
                )

            prompt = build_run_system_prompt(
                trust_db=db,
                repo_root=repo,
                workflow_phase="implementation",
                spec_digest="SPEC.md (sha256 deadbeef)\nRequirement: preserve API compatibility.",
            )

            self.assertIn("High-trust areas:", prompt)
            self.assertIn("src/core", prompt)
            self.assertIn("Low-trust areas:", prompt)
            self.assertIn("src/auth", prompt)
            self.assertIn("Patterns often corrected by developer:", prompt)
            self.assertIn("- security", prompt)
            self.assertIn("always_check_in: src/auth/*", prompt)
            self.assertIn("Always run tests after editing.", prompt)
            self.assertIn("Current phase is implementation.", prompt)
            self.assertIn("return a CheckInMessage JSON", prompt)
            self.assertIn("Recent qualitative guidance:", prompt)
            self.assertIn("Use existing auth adapter", prompt)
            self.assertIn("Approved specification context:", prompt)
            self.assertIn("preserve API compatibility", prompt)

    def test_build_prompt_retrieves_relevant_guidance_for_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "trust.db"
            db = TrustDB(db_path)
            repo = "/tmp/repo"

            db.replace_behavioral_guidelines(
                repo_root=repo,
                source="rules.md",
                guidelines=[
                    "Do not change response envelopes without approval.",
                    "Always run tests after editing.",
                ],
            )
            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="api.py",
                change_type="api",
                diff_size=4,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.5,
                user_decision="deny",
                user_feedback_text="Keep the existing response envelope for list endpoints.",
            )

            prompt = build_run_system_prompt(
                trust_db=db,
                repo_root=repo,
                workflow_phase="planning",
                task_text="Add filtering to the list endpoint while preserving the response envelope.",
            )

            self.assertIn("Do not change response envelopes without approval.", prompt)
            self.assertIn("Keep the existing response envelope for list endpoints.", prompt)


if __name__ == "__main__":
    unittest.main()
