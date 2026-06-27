"""Day 1 plugin walking-skeleton tests.

Smoke-test the PreToolUse adapter end-to-end: feed it a synthetic Claude
Code payload on stdin, parse the JSON it emits on stdout, assert the
decision shape. We do this by importing the script as a module to keep
the test fast and avoid subprocess overhead.

The aim is not to validate the scorer (existing tests do that) but to
confirm the adapter:
  * imports without pulling Bedrock or boto (Tier-0 invariant)
  * parses a Claude Code Edit/Write/MultiEdit payload
  * emits valid hookSpecificOutput JSON for proceed cases
  * stays silent (passthrough) for check-in cases
  * leaves non-governed tools untouched
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugin" / "bin"


def _load(name: str):
    """Import plugin/bin/<name>.py as a module under a unique alias."""
    path = _PLUGIN_BIN / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"plugin_{name.replace('-', '_')}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_decide(payload: dict) -> tuple[int, str]:
    decide = _load("hedwig-decide")
    buf = io.StringIO()
    sys.stdin = io.StringIO(json.dumps(payload))
    try:
        with redirect_stdout(buf):
            rc = decide.main()
    finally:
        sys.stdin = sys.__stdin__
    return rc, buf.getvalue()


def test_decide_runs_standalone_from_vendored_sc(tmp_path: Path) -> None:
    """The decide adapter must run as a standalone subprocess importing the
    bundled plugin/vendor/sc — not the parent research repo. This is the
    property that makes `/plugin install` real.

    We run it the way Claude Code does: a fresh `python3` subprocess with a
    cwd far from the repo and a scrubbed sys.path/PYTHONPATH so the real sc/
    is unreachable. If it still emits a valid decision, the vendored copy is
    self-sufficient.
    """
    import os
    import subprocess

    target = tmp_path / "tests" / "test_basic.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    payload = {
        "tool_name": "Edit",
        "cwd": str(tmp_path),
        "session_id": "s",
        "tool_input": {
            "file_path": str(target),
            "old_string": "1 + 1 == 2",
            "new_string": "2 + 2 == 4",
        },
    }

    # Scrub the repo from PYTHONPATH so the subprocess can only find sc/ via
    # the adapter's own vendor insertion.
    env = dict(os.environ)
    repo_root = str(_PLUGIN_BIN.parent.parent)
    env["PYTHONPATH"] = ""
    env.pop("VIRTUAL_ENV", None)

    proc = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(tmp_path),  # run far from the repo root
        env=env,
    )
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert proc.stdout, f"expected a decision, got nothing. stderr: {proc.stderr}"
    decision = json.loads(proc.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    # Sanity: the subprocess must not have leaned on the repo being on the path.
    assert repo_root not in env["PYTHONPATH"]


def test_tier0_no_cloud_imports() -> None:
    """Importing the decide adapter must not pull anthropic or boto.

    Snapshot sys.modules before and after so we measure what THIS import
    contributed — earlier tests in the suite may have imported cloud SDKs
    via paths we don't control (agent_client, ml_policy snapshots, etc.).
    """
    cloud_keys = ("anthropic", "boto", "bedrock")
    before = {m for m in sys.modules if any(k in m.lower() for k in cloud_keys)}
    # Drop any cached plugin module so spec_from_file_location re-executes.
    for cached in [k for k in sys.modules if k.startswith("plugin_hedwig_decide")]:
        del sys.modules[cached]
    _load("hedwig-decide")
    after = {m for m in sys.modules if any(k in m.lower() for k in cloud_keys)}
    pulled = sorted(after - before)
    assert not pulled, f"Tier-0 violation — decide adapter pulled cloud SDKs: {pulled}"


def test_non_governed_tool_passthrough() -> None:
    rc, out = _run_decide({"tool_name": "Read", "tool_input": {"file_path": "anywhere.py"}})
    assert rc == 0
    assert out == ""  # silent passthrough → native flow runs


def test_missing_payload_passthrough() -> None:
    rc, out = _run_decide({})
    assert rc == 0
    assert out == ""


def test_low_risk_test_edit_proceeds(tmp_path: Path) -> None:
    """Editing an EXISTING tests/ file is the textbook low-risk pattern.

    The heuristic assigns +0.3 to test_generation. With no is_new_file
    penalty (the file already exists), the score clears proceed_threshold=0.0
    and the adapter emits a permissionDecision: "allow", which suppresses
    Claude Code's native prompt.

    Brand-new test files intentionally fall through to a check-in — the
    first time a developer adds a new file, even a test, deserves review.
    """
    target = tmp_path / "tests" / "test_basic.py"
    target.parent.mkdir(parents=True)
    target.write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    payload = {
        "tool_name": "Edit",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(target),
            "old_string": "1 + 1 == 2",
            "new_string": "2 + 2 == 4",
        },
    }
    rc, out = _run_decide(payload)
    assert rc == 0
    assert out, "expected an allow decision, got silent passthrough"
    decision = json.loads(out)
    assert decision["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    # Plain-English judgment, not a debug dump: names the file, says it's
    # applying automatically.
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert "test_basic.py" in reason
    assert "automatically" in reason


def test_new_file_falls_through(tmp_path: Path) -> None:
    """Brand-new files fall through to native prompt regardless of pattern.

    Locks in the design choice: the new-file penalty is intentional, the
    first time a developer adds a path Hedwig hasn't seen before, the
    developer gets to look at it. Auto-trust is earned, not granted on
    creation.
    """
    target = tmp_path / "tests" / "test_new.py"
    target.parent.mkdir(parents=True)
    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(target),
            "content": "def test_ok():\n    assert True\n",
        },
    }
    rc, out = _run_decide(payload)
    assert rc == 0
    assert out == "", f"expected silent passthrough, got: {out!r}"


def test_security_sensitive_file_falls_through(tmp_path: Path) -> None:
    """Security-sensitive files must NOT auto-approve in Day 1 — they fall
    through to the native prompt so the developer reviews."""
    target = tmp_path / "auth.py"
    payload = {
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {
            "file_path": str(target),
            "content": "OAUTH_SECRET = 'rotate me'\n",
        },
    }
    rc, out = _run_decide(payload)
    assert rc == 0
    assert out == ""  # passthrough → native prompt fires


@pytest.mark.parametrize("tool_name", ["Edit", "MultiEdit"])
def test_edit_payload_shape_handled(tmp_path: Path, tool_name: str) -> None:
    """Edit and MultiEdit payloads have a different shape than Write —
    confirm the adapter handles both without raising."""
    target = tmp_path / "tests" / "test_x.py"
    target.parent.mkdir(parents=True)
    target.write_text("OLD\n")

    if tool_name == "Edit":
        tool_input = {
            "file_path": str(target),
            "old_string": "OLD",
            "new_string": "NEW",
        }
    else:
        tool_input = {
            "file_path": str(target),
            "edits": [{"old_string": "OLD", "new_string": "NEW"}],
        }

    rc, out = _run_decide({"tool_name": tool_name, "cwd": str(tmp_path), "tool_input": tool_input})
    assert rc == 0
    # Either silent passthrough or a valid hookSpecificOutput; never a crash.
    if out:
        json.loads(out)


@pytest.mark.parametrize("script", ["hedwig-decide", "hedwig-record", "hedwig-verify"])
@pytest.mark.parametrize("raw", ["[1, 2, 3]", '"a string"', "42", "true", "null"])
def test_non_dict_json_payload_never_crashes(tmp_path: Path, script: str, raw: str) -> None:
    """Valid JSON that isn't an object (list / string / number / bool / null)
    must not crash a hook with AttributeError on payload.get(...). All three
    classifier-touching hooks guard with isinstance(payload, dict) like
    declare.py does. Run as a real subprocess so the top-level escape path is
    exercised — the hook must exit 0 and emit no traceback."""
    import os
    import subprocess

    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
           "HEDWIG_NO_REEXEC": "1"}
    # verify only reaches the payload after a verify cmd is configured.
    if script == "hedwig-verify":
        env["HEDWIG_VERIFY_CMD"] = "true"
    proc = subprocess.run(
        ["python3", str(_PLUGIN_BIN / f"{script}.py")],
        input=raw, capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert proc.returncode == 0, f"{script} crashed on {raw!r}: {proc.stderr}"
    assert "Traceback" not in proc.stderr, f"{script} raised on {raw!r}: {proc.stderr}"
