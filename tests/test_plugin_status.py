"""SPINE 2 tests — decision logging + /hedwig-status tally.

Exercises the two-part flow:
  1. hedwig-decide.py appends a decision event (suppressed / surfaced) to
     ${CLAUDE_PLUGIN_DATA}/decisions.jsonl for every governed action.
  2. hedwig-status.py reads that log and reports the one-number headline.

Both run as subprocesses with CLAUDE_PLUGIN_DATA pointed at a tmp dir, so
the test mirrors how Claude Code invokes them and never touches the real
~/.claude data dir.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugin" / "bin"


def _run(script: str, *args: str, payload: dict | None = None, data_dir: Path, cwd: Path | None = None):
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    # Pin the canonical data-root scan to the same tmp dir so tests never read
    # the real ~/.claude/plugins/data/ (which would make "empty" tests fail).
    env["HEDWIG_DATA_ROOT"] = str(data_dir.parent)
    env["PYTHONPATH"] = ""
    env.pop("VIRTUAL_ENV", None)
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / script), *args],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _make_repo(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_basic.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    return proj


def test_suppressed_decision_is_logged(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_repo(tmp_path)
    payload = {
        "tool_name": "Edit",
        "cwd": str(proj),
        "session_id": "sess1",
        "tool_input": {
            "file_path": str(proj / "tests" / "test_basic.py"),
            "old_string": "1 + 1 == 2",
            "new_string": "2 + 2 == 4",
        },
    }
    proc = _run("hedwig-decide.py", payload=payload, data_dir=data_dir, cwd=proj)
    assert proc.returncode == 0, proc.stderr

    log = data_dir / "decisions.jsonl"
    assert log.exists()
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["verdict"] == "suppressed"
    assert rows[0]["session_id"] == "sess1"


def test_surfaced_decision_is_logged(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_repo(tmp_path)
    payload = {
        "tool_name": "Write",
        "cwd": str(proj),
        "session_id": "sess1",
        "tool_input": {
            "file_path": str(proj / "auth.py"),
            "content": "OAUTH_SECRET = 'x'\n",
        },
    }
    proc = _run("hedwig-decide.py", payload=payload, data_dir=data_dir, cwd=proj)
    assert proc.returncode == 0, proc.stderr
    # Security-sensitive → surfaced, and no stdout (silent passthrough).
    assert proc.stdout == ""

    rows = [json.loads(line) for line in (data_dir / "decisions.jsonl").read_text().splitlines() if line.strip()]
    assert rows[0]["verdict"] == "surfaced"


def test_status_tallies_session(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_repo(tmp_path)

    # One suppressed (edit existing test) + one surfaced (security write).
    _run(
        "hedwig-decide.py",
        payload={
            "tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
            "tool_input": {
                "file_path": str(proj / "tests" / "test_basic.py"),
                "old_string": "1 + 1 == 2", "new_string": "2 + 2 == 4",
            },
        },
        data_dir=data_dir, cwd=proj,
    )
    _run(
        "hedwig-decide.py",
        payload={
            "tool_name": "Write", "cwd": str(proj), "session_id": "s1",
            "tool_input": {"file_path": str(proj / "auth.py"), "content": "SECRET='x'\n"},
        },
        data_dir=data_dir, cwd=proj,
    )

    proc = _run("hedwig-status.py", "--session", "s1", "--json", data_dir=data_dir)
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert summary["total"] == 2
    assert summary["suppressed"] == 1
    assert summary["surfaced"] == 1
    assert summary["suppression_rate"] == 0.5


def test_status_filters_by_session(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_repo(tmp_path)
    for sess in ("sA", "sB"):
        _run(
            "hedwig-decide.py",
            payload={
                "tool_name": "Edit", "cwd": str(proj), "session_id": sess,
                "tool_input": {
                    "file_path": str(proj / "tests" / "test_basic.py"),
                    "old_string": "1 + 1 == 2", "new_string": "2 + 2 == 4",
                },
            },
            data_dir=data_dir, cwd=proj,
        )
    proc = _run("hedwig-status.py", "--session", "sA", "--json", data_dir=data_dir)
    summary = json.loads(proc.stdout)
    assert summary["total"] == 1  # only sA's decision, not sB's


def test_status_empty_is_graceful(tmp_path: Path) -> None:
    proc = _run("hedwig-status.py", data_dir=tmp_path / "nonexistent")
    assert proc.returncode == 0
    assert "No edits governed yet" in proc.stdout


def test_status_dashboard_shows_surfaced_reason(tmp_path: Path) -> None:
    """The dashboard's 'why it surfaced these' section must carry the
    plain-English reason for a surfaced edit — that's the legibility surface."""
    data_dir = tmp_path / "data"
    proj = _make_repo(tmp_path)
    # A security-sensitive write surfaces with a plain-English reason.
    _run(
        "hedwig-decide.py",
        payload={
            "tool_name": "Write", "cwd": str(proj), "session_id": "s1",
            "tool_input": {"file_path": str(proj / "auth_token.py"), "content": "SECRET=1\n"},
        },
        data_dir=data_dir, cwd=proj,
    )
    proc = _run("hedwig-status.py", "--session", "s1", data_dir=data_dir)
    assert proc.returncode == 0
    assert "Why it surfaced these" in proc.stdout
    assert "auth_token.py" in proc.stdout
    assert "security-sensitive" in proc.stdout


def test_reason_is_plain_english_not_debug(tmp_path: Path) -> None:
    """Auto-apply reason must read as judgment, not a score dump."""
    data_dir = tmp_path / "data"
    proj = _make_repo(tmp_path)
    proc = _run(
        "hedwig-decide.py",
        payload={
            "tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
            "tool_input": {
                "file_path": str(proj / "tests" / "test_basic.py"),
                "old_string": "1 + 1 == 2", "new_string": "2 + 2 == 4",
            },
        },
        data_dir=data_dir, cwd=proj,
    )
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    # No debug tokens.
    assert "score=" not in reason
    assert "blast=" not in reason
    # Reads like a sentence.
    assert reason.endswith(".")
