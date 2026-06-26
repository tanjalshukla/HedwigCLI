"""The learned-scorer interpreter shim (ensure_learned_interpreter).

Claude Code launches the hooks under a bare `python3` that usually lacks
numpy/scikit-learn, so the classifier silently degrades. The shim re-execs the
classifier-touching hooks under a deps-capable interpreter ($HEDWIG_PYTHON or
~/.hedwig/venv) so the learned scorer ALWAYS runs at the booth.

These tests pin the contract: re-exec happens (and materializes a real
classifier) when launched deps-free with a capable target; and the guardrails
hold — opt-out, sentinel-no-loop, and no-needless-reexec when already capable.

NOTE: the suite-wide conftest sets HEDWIG_NO_REEXEC for hermeticity. Each test
here builds its OWN subprocess env from scratch (NOT os.environ) so it controls
the shim explicitly rather than inheriting that pin.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugin" / "bin"
_VENV_PYTHON = Path(sys.executable)  # the test venv HAS numpy+sklearn

# A bare interpreter without the learned-scorer deps, to launch the hook under.
# /usr/bin/python3 on macOS/Linux is the canonical "no site-packages" case.
_BARE_PYTHON = "/usr/bin/python3"


def _bare_has_no_deps() -> bool:
    try:
        r = subprocess.run([_BARE_PYTHON, "-c", "import sklearn"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode != 0
    except Exception:
        return True


requires_split = pytest.mark.skipif(
    not Path(_BARE_PYTHON).exists() or not _bare_has_no_deps(),
    reason="needs a bare /usr/bin/python3 that lacks sklearn (the booth scenario)",
)


def _make_git_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "calc.py").write_text("def add(a, b):\n    return a+b\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=str(proj), env=env, capture_output=True, check=True)
    return proj


def _decide_payload(proj: Path) -> str:
    return json.dumps({
        "tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
        "tool_input": {"file_path": str(proj / "src/calc.py"),
                       "old_string": "a+b", "new_string": "a + b"},
    })


@requires_split
def test_reexec_materializes_classifier_when_launched_depfree(tmp_path: Path) -> None:
    """Launched under a bare interpreter (no sklearn) with HEDWIG_PYTHON
    pointing at a capable one, the decide hook re-execs and the LEARNED path
    runs — proven by a PolicyClassifier blob landing in trust.db, which the
    pure-heuristic path never writes."""
    data_dir = tmp_path / "data"
    proj = _make_git_proj(tmp_path)

    # Bare env: no inherited PATH tricks, no HEDWIG_NO_REEXEC. Point the shim at
    # this test venv's interpreter (which has numpy+sklearn).
    env = {
        "PATH": "/usr/bin:/bin",
        "CLAUDE_PLUGIN_DATA": str(data_dir),
        "HEDWIG_PYTHON": str(_VENV_PYTHON),
    }
    proc = subprocess.run(
        [_BARE_PYTHON, str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=_decide_payload(proj), capture_output=True, text=True,
        cwd=str(proj), env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "decide must emit a verdict"

    # The smoking gun: a persisted classifier. Only the learned path writes it.
    import sqlite3
    conn = sqlite3.connect(str(data_dir / "trust.db"))
    try:
        rows = conn.execute(
            "SELECT length(model_blob) FROM policy_models"
        ).fetchall()
    finally:
        conn.close()
    assert rows and rows[0][0] > 0, (
        "no PolicyClassifier persisted — the shim did not engage the learned "
        "path under the deps-free launch"
    )


@requires_split
def test_no_infinite_loop_when_target_also_lacks_deps(tmp_path: Path) -> None:
    """If HEDWIG_PYTHON points at ANOTHER deps-free interpreter, the shim must
    re-exec at most once (sentinel guard) then run in place and degrade — never
    loop. A timeout here would mean an exec loop."""
    data_dir = tmp_path / "data"
    proj = _make_git_proj(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin",
        "CLAUDE_PLUGIN_DATA": str(data_dir),
        "HEDWIG_PYTHON": _BARE_PYTHON,  # also lacks sklearn
    }
    proc = subprocess.run(
        [_BARE_PYTHON, str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=_decide_payload(proj), capture_output=True, text=True,
        cwd=str(proj), env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "must still emit a verdict after degrading"
    assert "traceback" not in proc.stderr.lower()
    assert "modulenotfound" not in proc.stderr.lower()


@requires_split
def test_opt_out_keeps_current_interpreter(tmp_path: Path) -> None:
    """HEDWIG_NO_REEXEC pins the hook to the launching interpreter even when a
    capable target is configured — so it stays deps-free and degrades (no
    classifier persisted)."""
    data_dir = tmp_path / "data"
    proj = _make_git_proj(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin",
        "CLAUDE_PLUGIN_DATA": str(data_dir),
        "HEDWIG_PYTHON": str(_VENV_PYTHON),  # capable, but…
        "HEDWIG_NO_REEXEC": "1",             # …opted out
    }
    proc = subprocess.run(
        [_BARE_PYTHON, str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=_decide_payload(proj), capture_output=True, text=True,
        cwd=str(proj), env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "must emit a verdict on the heuristic path"
    # Opted out + deps-free launch → heuristic only → no classifier blob.
    db = data_dir / "trust.db"
    if db.exists():
        import sqlite3
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute("SELECT count(*) FROM policy_models").fetchall()
        finally:
            conn.close()
        assert rows[0][0] == 0, "opt-out should not have engaged the learned path"


if __name__ == "__main__":
    import pytest as _p
    _p.main([__file__, "-v"])
