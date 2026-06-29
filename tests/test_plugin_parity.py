"""CLI→plugin parity surfaces added to close the capability gap audit:

- /hedwig-learn active        — view confirmed/active preferences (CLI /prefs)
- /hedwig-observe report       — repo-activity summary (CLI /observe report)
- /hedwig-observe memory       — stored repo memory (CLI /context)
- /hedwig-observe cochange     — files that change together (CLI /cochange),
                                 grouped by session_id on the plugin

Run as subprocesses the way Claude Code invokes the slash commands, keying the
seed on the SAME canonical repo_root the bins read (repo_root_key) so the
fixtures and the commands agree.
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
if str(_PLUGIN_BIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_BIN))


def _env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    env["HEDWIG_NO_REEXEC"] = "1"
    env["PYTHONPATH"] = ""
    return env


def _canonical(repo: Path) -> str:
    """The key the bins derive via repo_root_key(None) when run with cwd=repo."""
    from _hedwig_common import repo_root_key

    return repo_root_key(str(repo))


def _run(bin_name: str, args: list[str], data_dir: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / bin_name), *args],
        capture_output=True, text=True, cwd=str(cwd), env=_env(data_dir),
    )


def test_learn_active_lists_confirmed_prefs(tmp_path: Path) -> None:
    from sc.trust_db import TrustDB

    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir.mkdir(parents=True)
    db = TrustDB(data_dir / "trust.db")
    db.save_confirmed_preference(
        repo_root=_canonical(repo), session_id="s",
        preference_json=json.dumps({"accepted": True, "driver": "scope_constraint", "preference": None}),
        driver="scope_constraint",
    )
    out = _run("hedwig-learn.py", ["active"], data_dir, repo)
    assert out.returncode == 0, out.stderr
    assert "active preferences" in out.stdout.lower()
    assert "no confirmed preferences" not in out.stdout.lower()


def test_learn_active_empty_is_friendly(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir.mkdir(parents=True)
    out = _run("hedwig-learn.py", ["active"], data_dir, repo)
    assert out.returncode == 0, out.stderr
    assert "no confirmed preferences" in out.stdout.lower()


def test_observe_report_summarizes(tmp_path: Path) -> None:
    from sc.trust_db import TrustDB

    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir.mkdir(parents=True)
    db = TrustDB(data_dir / "trust.db")
    rk = _canonical(repo)
    db.add_logic_notes(rk, source="x", notes=["tests live in tests/"], files=["tests/"])
    out = _run("hedwig-observe.py", ["report"], data_dir, repo)
    assert out.returncode == 0, out.stderr
    assert "repo activity" in out.stdout.lower()
    assert "1 repo fact" in out.stdout


def test_observe_memory_shows_stored(tmp_path: Path) -> None:
    from sc.trust_db import TrustDB

    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir.mkdir(parents=True)
    db = TrustDB(data_dir / "trust.db")
    rk = _canonical(repo)
    db.add_behavioral_guidelines(rk, source="x", guidelines=["prefer small functions"])
    out = _run("hedwig-observe.py", ["memory"], data_dir, repo)
    assert out.returncode == 0, out.stderr
    assert "prefer small functions" in out.stdout


def test_observe_cochange_groups_by_session(tmp_path: Path) -> None:
    from sc.trust_db import TrustDB

    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir.mkdir(parents=True)
    db = TrustDB(data_dir / "trust.db")
    rk = _canonical(repo)
    # Two files edited together across 3 sessions → co-change by session_id.
    for s in ("a", "b", "c"):
        for f in ("models.py", "store.py"):
            db.record_trace(
                repo_root=rk, session_id=s, task=rk, stage="apply",
                action_type="write", file_path=f,
                change_type="general_change:existing", diff_size=8, blast_radius=1,
                existing_lease=False, lease_type=None, prior_approvals=0,
                prior_denials=0, policy_action="proceed", policy_score=1.0,
                user_decision="auto_approve",
            )
    out = _run("hedwig-observe.py", ["cochange"], data_dir, repo)
    assert out.returncode == 0, out.stderr
    assert "models.py" in out.stdout and "store.py" in out.stdout, out.stdout


def test_all_parity_commands_exit_zero_on_empty(tmp_path: Path) -> None:
    """Empty-state must be friendly, never a crash."""
    data_dir = tmp_path / "data"
    repo = tmp_path / "repo"
    repo.mkdir()
    data_dir.mkdir(parents=True)
    for binname, args in [
        ("hedwig-learn.py", ["active"]),
        ("hedwig-observe.py", ["report"]),
        ("hedwig-observe.py", ["memory"]),
        ("hedwig-observe.py", ["cochange"]),
    ]:
        out = _run(binname, args, data_dir, repo)
        assert out.returncode == 0, f"{binname} {args}: {out.stderr}"
        assert out.stdout.strip()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
