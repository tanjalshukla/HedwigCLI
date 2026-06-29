"""Plugin hypothesis noticer — hedwig-notice.py.

The plugin's semantic noticer: Claude Code (via the repo-hypotheses skill) reads
this session's traces and proposes candidate preferences/guidelines/facts; this
bin validates citations against real traces and ingests them into the hypothesis
bank as PENDING candidates (reusing sc.hypothesis_bank.ingest_llm_hypotheses,
the same path the CLI noticer uses). The grounding gate — every candidate must
cite a real trace ID — is the anti-hallucination invariant.

Run as subprocesses the way Claude Code invokes the skill, with isolated
CLAUDE_PLUGIN_DATA.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
_PLUGIN_BIN = _PLUGIN / "bin"
_VENDOR = _PLUGIN / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def _env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    env["HEDWIG_NO_REEXEC"] = "1"
    env["PYTHONPATH"] = ""
    return env


def _seed_traces(data_dir: Path, repo: str, session: str, n: int = 4) -> list[int]:
    """Seed n apply traces; return their real trace IDs."""
    from sc.trust_db import TrustDB

    data_dir.mkdir(parents=True, exist_ok=True)
    db = TrustDB(data_dir / "trust.db")
    for i in range(n):
        db.record_trace(
            repo_root=repo, session_id=session, task="t", stage="apply",
            action_type="write", file_path=f"svc/f{i}.py",
            change_type="general_change:existing", diff_size=10, blast_radius=1,
            existing_lease=False, lease_type=None, prior_approvals=0,
            prior_denials=0, policy_action="check_in", policy_score=0.4,
            user_decision="deny", pushback_type="scope_constraint",
        )
    return [int(t["id"]) for t in db.session_traces(repo, session)]


def _notice(payload: dict, data_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-notice.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=_env(data_dir),
    )


def test_traces_mode_lists_ids(tmp_path: Path) -> None:
    """Step 1: the traces digest prints, so the agent has [id]s to cite."""
    data_dir = tmp_path / "data"
    repo = str(tmp_path / "repo")
    _seed_traces(data_dir, repo, "s1")
    out = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-notice.py"), "traces", repo, "s1"],
        capture_output=True, text=True, env=_env(data_dir),
    )
    assert out.returncode == 0, out.stderr
    assert "[" in out.stdout and "svc/f0.py" in out.stdout


def test_grounded_candidate_is_recorded(tmp_path: Path) -> None:
    """A candidate citing a real trace ID lands in the bank (pending)."""
    data_dir = tmp_path / "data"
    repo = str(tmp_path / "repo")
    ids = _seed_traces(data_dir, repo, "s1")
    out = _notice({
        "cwd": repo, "session_id": "s1",
        "candidates": [{
            "type": "preference",
            "text": "Pause before edits to svc/",
            "driver": "pause_svc",
            "rationale": "developer narrowed scope repeatedly",
            "evidence_trace_ids": ids[:2],
        }],
    }, data_dir)
    assert out.returncode == 0, out.stderr
    assert "recorded 1 grounded candidate" in out.stdout

    # Confirm it's actually pending in the bank.
    from sc.trust_db import TrustDB
    db = TrustDB(data_dir / "trust.db")
    pending = db.get_pending_hypothesis_candidates(repo, "s1")
    drivers = {(c["driver"] if not hasattr(c, "driver") else c.driver) for c in pending}
    assert "pause_svc" in drivers, f"candidate must be in the bank; got {drivers}"


def test_uncited_candidate_is_dropped(tmp_path: Path) -> None:
    """The anti-hallucination gate: a candidate citing no real trace is dropped."""
    data_dir = tmp_path / "data"
    repo = str(tmp_path / "repo")
    _seed_traces(data_dir, repo, "s1")
    out = _notice({
        "cwd": repo, "session_id": "s1",
        "candidates": [{
            "type": "preference",
            "text": "Invented rule with no evidence",
            "driver": "hallucinated",
            "rationale": "made up",
            "evidence_trace_ids": [999999],  # not a real trace
        }],
    }, data_dir)
    assert out.returncode == 0, out.stderr
    assert "recorded 0 grounded candidate" in out.stdout


def test_garbage_input_exits_zero(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    out = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-notice.py")],
        input="not json {{{", capture_output=True, text=True, env=_env(data_dir),
    )
    assert out.returncode == 0


def test_no_candidates_is_friendly(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo = str(tmp_path / "repo")
    _seed_traces(data_dir, repo, "s1")
    out = _notice({"cwd": repo, "session_id": "s1", "candidates": []}, data_dir)
    assert out.returncode == 0, out.stderr
    assert "no candidates" in out.stdout.lower()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
