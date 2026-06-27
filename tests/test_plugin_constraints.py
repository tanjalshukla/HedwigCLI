"""Hard-constraint enforcement + authoring on the plugin path (Part C).

Cascade layer 1: a developer-set hard constraint overrides the scorer. These
run the hooks as subprocesses (the way Claude Code does), with an isolated
CLAUDE_PLUGIN_DATA so each test gets its own trust.db. We prove:

  * /hedwig-rules add writes a constraint that /hedwig-rules list reads back;
  * an always_deny constraint makes hedwig-decide BLOCK a matching edit
    (permissionDecision: "deny") even when the scorer alone would auto-apply;
  * a non-matching edit is unaffected (still auto-applies);
  * always_allow forces an allow.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugin" / "bin"


def _env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    env["HEDWIG_NO_REEXEC"] = "1"  # pin the interpreter (hermetic)
    return env


def _rules(data_dir: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-rules.py"), *args],
        capture_output=True, text=True, cwd=str(cwd), env=_env(data_dir),
    )


def _decide(data_dir: Path, *, proj: Path, rel: str) -> subprocess.CompletedProcess:
    payload = {
        "tool_name": "Edit",
        "cwd": str(proj),
        "session_id": "s1",
        "tool_input": {
            "file_path": str(proj / rel),
            "old_string": "x = 1",
            "new_string": "x = 2",
        },
    }
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=str(proj), env=_env(data_dir),
    )


def _make_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "config" / "prod").mkdir(parents=True)
    (proj / "config" / "prod" / "settings.py").write_text("x = 1\n")
    (proj / "src").mkdir()
    (proj / "src" / "app.py").write_text("x = 1\n")
    return proj


def test_rules_add_and_list_roundtrip(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)

    add = _rules(data_dir, "add", "deny", "config/prod/**", cwd=proj)
    assert add.returncode == 0, add.stderr
    assert "Added" in add.stdout and "config/prod/**" in add.stdout

    listing = _rules(data_dir, "list", cwd=proj)
    assert listing.returncode == 0, listing.stderr
    assert "always_deny" in listing.stdout
    assert "config/prod/**" in listing.stdout


def test_deny_constraint_blocks_matching_edit(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    _rules(data_dir, "add", "deny", "config/prod/**", cwd=proj)

    out = _decide(data_dir, proj=proj, rel="config/prod/settings.py")
    assert out.returncode == 0, out.stderr
    assert out.stdout, "a denied edit must emit a decision"
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "hard constraint" in decision["permissionDecisionReason"]


def test_constraint_does_not_affect_other_paths(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    _rules(data_dir, "add", "deny", "config/prod/**", cwd=proj)

    # An edit OUTSIDE the constrained path must not be denied by the constraint.
    out = _decide(data_dir, proj=proj, rel="src/app.py")
    assert out.returncode == 0, out.stderr
    if out.stdout:
        decision = json.loads(out.stdout)["hookSpecificOutput"]
        # Either an allow (auto-apply) or no deny — never blocked by a
        # constraint that doesn't match this path.
        assert decision["permissionDecision"] != "deny"


def test_always_allow_constraint_forces_allow(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    _rules(data_dir, "add", "allow", "src/**", cwd=proj)

    out = _decide(data_dir, proj=proj, rel="src/app.py")
    assert out.returncode == 0, out.stderr
    assert out.stdout, "an always_allow edit must emit an allow decision"
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "allow"
    assert "hard constraint" in decision["permissionDecisionReason"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
