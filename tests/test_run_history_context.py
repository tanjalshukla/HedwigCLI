from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from sc.policy import PolicyDecision
from sc.run.helpers import _autonomy_history_context
from sc.run.ui import _render_auto_approve_summary
from sc.trust_db import TrustDB


class RunHistoryContextTests(unittest.TestCase):
    def test_history_context_surfaces_quant_and_qual_signals_compactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.replace_behavioral_guidelines(
                repo_root=repo,
                source="manual_rule",
                guidelines=[
                    "For routine validation and service-layer changes, continue autonomously; only check in for API, schema, or security changes.",
                ],
            )
            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="read",
                action_type="read_request",
                file_path="task_api/api.py",
                change_type=None,
                diff_size=None,
                blast_radius=2,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.3,
                user_decision="approve",
                response_time_ms=8000,
            )
            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="read",
                action_type="read_request",
                file_path="task_api/service.py",
                change_type=None,
                diff_size=None,
                blast_radius=2,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.3,
                user_decision="approve",
                response_time_ms=10000,
            )

            files = ["task_api/api.py", "task_api/service.py"]
            histories = {
                path: db.policy_history(repo, path, stage="read")
                for path in files
            }
            policies = {
                path: PolicyDecision(
                    action="proceed",
                    score=1000.0,
                    reasons=("active read lease",),
                )
                for path in files
            }

            context = _autonomy_history_context(
                trust_db=db,
                repo_root=repo,
                stage="read",
                task="Extend the summary flow with a priority filter while preserving the API.",
                files=files,
                histories=histories,
                policies=policies,
            )

            self.assertIsNotNone(context)
            assert context is not None
            self.assertIn("reused prior read access on 2/2 files", context.quantitative or "")
            self.assertIn("prior approvals 2", context.quantitative or "")
            self.assertIn("prior denials 0", context.quantitative or "")
            self.assertIn("guidance:", context.qualitative or "")
            self.assertIn("continue autonomously", context.qualitative or "")

    def test_history_context_prefers_developer_guidance_over_static_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.replace_behavioral_guidelines(
                repo_root=repo,
                source="manual_rule",
                guidelines=[
                    "For routine backend changes, reuse existing validation helpers and avoid creating new files unless clearly necessary.",
                ],
            )
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
                response_time_ms=9000,
                user_feedback_text=(
                    "Use the nested object response. Preserve the existing response envelope "
                    "and all public handler signatures. Continue autonomously for low-risk "
                    "internal changes; only check in for API, interface, schema, or security changes."
                ),
                check_in_initiator="model_proactive",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="read",
                action_type="read_request",
                file_path="task_api/api.py",
                change_type=None,
                diff_size=None,
                blast_radius=2,
                existing_lease=True,
                lease_type="always_allow",
                prior_approvals=0,
                prior_denials=0,
                policy_action="proceed",
                policy_score=1.0,
                user_decision="auto_approve_read_lease",
                response_time_ms=None,
            )

            files = ["task_api/api.py"]
            histories = {path: db.policy_history(repo, path, stage="read") for path in files}
            policies = {
                path: PolicyDecision(
                    action="proceed",
                    score=1000.0,
                    reasons=("active read lease",),
                )
                for path in files
            }

            context = _autonomy_history_context(
                trust_db=db,
                repo_root=repo,
                stage="read",
                task="Extend the summary flow with an optional priority filter while preserving signatures.",
                files=files,
                histories=histories,
                policies=policies,
            )

            self.assertIsNotNone(context)
            assert context is not None
            self.assertIn("guidance:", context.qualitative or "")
            self.assertIn("low-risk internal changes", context.qualitative or "")
            self.assertNotIn("reuse existing validation helpers", context.qualitative or "")

    def test_history_context_omits_output_without_prior_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            context = _autonomy_history_context(
                trust_db=db,
                repo_root=repo,
                stage="read",
                task="Read two files for a new task.",
                files=["a.py", "b.py"],
                histories={},
                policies={},
            )
            self.assertIsNone(context)

    def test_render_auto_approve_summary_stays_compact(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            _render_auto_approve_summary(
                "read",
                "reused prior read access on 2/2 files; prior approvals 2; prior denials 0; avg review 9.0s",
                'guidance: "For routine validation and service-layer changes, continue autonomously; only check in for API, schema, or security changes."',
                None,
            )

        output = stream.getvalue()
        self.assertIn("reused prior read access on 2/2 files", output)
        self.assertNotIn("Quant:", output)
        self.assertNotIn("Qual:", output)


if __name__ == "__main__":
    unittest.main()
