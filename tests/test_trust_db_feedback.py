from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.trust_db import TrustDB


class TrustDBFeedbackTests(unittest.TestCase):
    def test_recent_feedback_snippets_returns_unique_recent_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="a.py",
                change_type="logic",
                diff_size=4,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.4,
                user_decision="deny",
                user_feedback_text="Use the existing error wrapper.",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s2",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="b.py",
                change_type="logic",
                diff_size=2,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.3,
                user_decision="deny",
                user_feedback_text="Use the existing error wrapper.",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s3",
                task="task",
                stage="check_in",
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
                user_feedback_text="Prefer cursor pagination for API consistency.",
            )

            snippets = db.recent_feedback_snippets(repo, limit=4)
            self.assertEqual(len(snippets), 2)
            self.assertIn("Prefer cursor pagination for API consistency.", snippets)
            self.assertIn("Use the existing error wrapper.", snippets)

    def test_relevant_feedback_snippets_prefers_semantic_match_over_recency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="src/api/errors.py",
                change_type="error_handling",
                diff_size=8,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.6,
                user_decision="deny",
                user_feedback_text="Use AppError with explicit error codes for API failures.",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s2",
                task="task",
                stage="check_in",
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
                user_feedback_text="Prefer a dedicated summary endpoint over expanding the list response.",
            )

            snippets = db.relevant_feedback_snippets(
                repo,
                query_text="Improve API error handling and keep explicit error codes in responses.",
                limit=2,
            )

            self.assertGreaterEqual(len(snippets), 1)
            self.assertEqual(snippets[0], "Use AppError with explicit error codes for API failures.")

    def test_relevant_behavioral_guidelines_prefers_semantic_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.replace_behavioral_guidelines(
                repo_root=repo,
                source="rules.md",
                guidelines=[
                    "Do not change response envelopes without approval.",
                    "Always run tests after editing demo repo files.",
                ],
            )

            guidelines = db.relevant_behavioral_guidelines(
                repo,
                query_text="Add API filtering while preserving the response envelope.",
                limit=2,
            )

            self.assertGreaterEqual(len(guidelines), 1)
            self.assertEqual(guidelines[0].guideline, "Do not change response envelopes without approval.")

    def test_relevant_logic_notes_prefers_functional_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.add_logic_notes(
                repo,
                source="run_summary",
                notes=[
                    "Added a dedicated summary endpoint instead of changing the existing list response envelope.",
                    "Validation changes in the service layer should stay local and avoid API surface changes.",
                ],
                files=["task_api/api.py", "task_api/service.py"],
                change_types=["api_change", "error_handling"],
            )

            notes = db.relevant_logic_notes(
                repo,
                query_text="Add a summary route while keeping the existing list response envelope intact.",
                limit=2,
            )

            self.assertGreaterEqual(len(notes), 1)
            self.assertEqual(
                notes[0].note,
                "Added a dedicated summary endpoint instead of changing the existing list response envelope.",
            )


if __name__ == "__main__":
    unittest.main()
