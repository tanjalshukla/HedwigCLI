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


def test_constraint_fires_across_symlinked_cwd(tmp_path: Path) -> None:
    """Regression: a rule added via /hedwig-rules (which keys on the resolved
    getcwd) must still fire in hedwig-decide when Claude Code passes an
    UNRESOLVED payload["cwd"] (symlinked path). Before repo_root_key the two
    keyed differently and the always_deny silently didn't fire — a wrong-verdict
    bug. Here the real dir lives under tmp_path; we drive decide with a symlink
    to it and assert the deny still blocks."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)

    # A symlink pointing at the real project dir.
    link = tmp_path / "linked_proj"
    try:
        link.symlink_to(proj, target_is_directory=True)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("symlinks not supported on this platform")

    # Author the rule via the symlinked cwd (slash command resolves it).
    add = _rules(data_dir, "add", "deny", "config/prod/**", cwd=link)
    assert add.returncode == 0, add.stderr

    # Decide receives the UNRESOLVED symlink path in the payload, as Claude Code
    # would. repo_root_key resolves both sides to the same key, so the deny fires.
    payload = {
        "tool_name": "Edit", "cwd": str(link), "session_id": "s1",
        "tool_input": {"file_path": str(link / "config/prod/settings.py"),
                       "old_string": "x = 1", "new_string": "x = 2"},
    }
    out = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=str(link), env=_env(data_dir),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout, "the constraint should produce a deny decision"
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny", (
        "constraint must fire despite the symlinked cwd (repo_root_key canonicalization)"
    )


def test_malformed_payloads_never_crash_decide(tmp_path: Path) -> None:
    """A PreToolUse hook must exit 0 on ANY valid-JSON payload, even malformed
    shapes (non-dict tool_input, non-string file_path, non-list edits). A crash
    here would block the user's edit."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    bad_payloads = [
        {"tool_name": "Edit", "cwd": str(proj), "tool_input": "not a dict"},
        {"tool_name": "Edit", "cwd": str(proj), "tool_input": {"file_path": 123}},
        {"tool_name": "MultiEdit", "cwd": str(proj),
         "tool_input": {"file_path": str(proj / "src/app.py"), "edits": "not a list"}},
        {"tool_name": "MultiEdit", "cwd": str(proj),
         "tool_input": {"file_path": str(proj / "src/app.py"), "edits": [42, "x"]}},
    ]
    for p in bad_payloads:
        out = subprocess.run(
            ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
            input=json.dumps(p), capture_output=True, text=True,
            cwd=str(proj), env=_env(data_dir),
        )
        assert out.returncode == 0, f"decide crashed on {p}: {out.stderr}"
        assert "Traceback" not in out.stderr, f"decide raised on {p}: {out.stderr}"


def test_malformed_payloads_never_crash_record(tmp_path: Path) -> None:
    """PostToolUse must exit 0 on malformed shapes too."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    for ti in ("not a dict", {"file_path": 123}, {"file_path": None}):
        out = subprocess.run(
            ["python3", str(_PLUGIN_BIN / "hedwig-record.py")],
            input=json.dumps({"tool_name": "Edit", "cwd": str(proj), "tool_input": ti}),
            capture_output=True, text=True, cwd=str(proj), env=_env(data_dir),
        )
        assert out.returncode == 0, f"record crashed on tool_input={ti!r}: {out.stderr}"
        assert "Traceback" not in out.stderr


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
