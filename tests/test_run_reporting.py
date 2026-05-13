from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from sc.run.reporting import _render_run_summary
from sc.trust_db import TrustDB


class RunReportingTests(unittest.TestCase):
    def test_run_summary_stays_user_facing_and_compact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"
            session_id = "session-1"

            db.record_trace(
                repo_root=repo,
                session_id=session_id,
                task="task",
                stage="planning",
                action_type="check_in",
                file_path="__session__",
                change_type=None,
                diff_size=None,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.2,
                user_decision="approve",
                response_time_ms=7000,
            )
            db.record_trace(
                repo_root=repo,
                session_id=session_id,
                task="task",
                stage="apply",
                action_type="propose_update",
                file_path="task_api/api.py",
                change_type="general_change",
                diff_size=24,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.2,
                user_decision="approve",
                response_time_ms=7000,
            )

            stream = io.StringIO()
            with redirect_stdout(stream):
                _render_run_summary(
                    trust_db=db,
                    repo_root=repo,
                    session_id=session_id,
                )

            output = stream.getvalue()
            # Panel title → "run complete"
            self.assertIn("run complete", output)
            # Updated files list surfaces the changed file
            self.assertIn("task_api/api.py", output)
            # Plain-English "no pauses" language replaces initiator-style jargon
            self.assertIn("No pauses", output)
            # Jargon we explicitly stripped should stay out
            self.assertNotIn("Change patterns:", output)
            self.assertNotIn("general_change", output)
            self.assertNotIn("policy-initiated", output)
            self.assertNotIn("model-initiated", output)
            # Cold-start messaging is still there, but in plain English now
            self.assertIn("cold-start", output)
            self.assertNotIn("Session id=", output)
            self.assertNotIn("rubber-stamp approvals", output)
            self.assertNotIn("Developer feedback events", output)


if __name__ == "__main__":
    unittest.main()
