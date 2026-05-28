from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from sc.policy import PolicyDecision
from sc.run.traces import _record_traces
from sc.trust_db import PolicyHistory, TrustDB


def test_revise_feedback_marks_every_file_scope_constraint() -> None:
    repo = "/repo"
    session_id = "session"
    files = ["service.py", "api.py"]
    history = PolicyHistory(
        approvals=0,
        denials=0,
        effective_approvals=0.0,
        rubber_stamp_approvals=0,
        avg_response_ms=None,
        avg_edit_distance=None,
    )
    policy = PolicyDecision(
        action="check_in",
        score=0.0,
        reasons=("test",),
    )

    with TemporaryDirectory() as tmp:
        db = TrustDB(Path(tmp) / "trust.db")
        _record_traces(
            trust_db=db,
            repo_root=repo,
            session_id=session_id,
            task="add tag filtering",
            stage="apply",
            action_type="write_request",
            files=files,
            histories={path: history for path in files},
            policies={path: policy for path in files},
            user_decision="deny",
            response_time_ms=1000,
            change_types={path: "general_change" for path in files},
            diff_sizes={path: 1 for path in files},
            blast_radius=2,
            existing_leases={path: None for path in files},
            user_feedback_text="[revise] just service.py",
        )

        rows = db.session_traces(repo, session_id)

    assert [row["pushback_type"] for row in rows] == [
        "scope_constraint",
        "scope_constraint",
    ]
    assert [row["user_feedback_text"] for row in rows] == [
        "just service.py",
        "just service.py",
    ]
