#!/usr/bin/env python3
"""End-to-end plugin harness for agent-driven QA.

Simulates the full Claude Code hook cycle (PreToolUse → PostToolUse) against
a real isolated copy of demo_recipe_api, without needing a running Claude Code
instance. Agents can run this to verify that the plugin governance layer
works correctly after any change.

Usage:
    python3 tooling/test_plugin_harness.py            # run all scenarios
    python3 tooling/test_plugin_harness.py --verbose  # show hook stdout/stderr
    python3 tooling/test_plugin_harness.py --scenario low_risk_edit

Each scenario fires hedwig-decide.py and hedwig-record.py with a realistic
payload, then asserts the expected verdict (suppressed / surfaced / blocked).

Exit 0 = all pass. Exit 1 = failures (printed to stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_BIN = REPO_ROOT / "plugin" / "bin"
DEMO_SRC = REPO_ROOT / "demo_recipe_api"

# Use the local plugin/bin scripts directly (not the installed copy) so the
# harness always tests the latest code in this repo.
DECIDE_SCRIPT = PLUGIN_BIN / "hedwig-decide.py"
RECORD_SCRIPT = PLUGIN_BIN / "hedwig-record.py"

# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def _edit_payload(
    file_path: str,
    old_string: str,
    new_string: str,
    cwd: str,
    session_id: str = "harness-session",
) -> dict:
    return {
        "session_id": session_id,
        "tool_name": "Edit",
        "tool_input": {
            "file_path": file_path,
            "old_string": old_string,
            "new_string": new_string,
        },
        "cwd": cwd,
    }


def _write_payload(
    file_path: str,
    content: str,
    cwd: str,
    session_id: str = "harness-session",
) -> dict:
    return {
        "session_id": session_id,
        "tool_name": "Write",
        "tool_input": {
            "file_path": file_path,
            "content": content,
        },
        "cwd": cwd,
    }


# ---------------------------------------------------------------------------
# Hook runner
# ---------------------------------------------------------------------------

def _run_hook(script: Path, payload: dict, data_dir: Path, verbose: bool) -> tuple[int, dict | None, str]:
    """Run a hook script with the given payload on stdin.

    Returns (returncode, parsed_stdout_json_or_None, stderr_text).
    """
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    env["HEDWIG_NO_REEXEC"] = "1"  # skip venv re-exec in harness
    # Ensure the vendored sc/ is importable from the local plugin/bin path.
    env["PYTHONPATH"] = str(PLUGIN_BIN.parent / "vendor") + ":" + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )

    if verbose:
        if result.stdout.strip():
            print(f"  [stdout] {result.stdout.strip()}")
        if result.stderr.strip():
            print(f"  [stderr] {result.stderr.strip()}")

    stdout_json = None
    if result.stdout.strip():
        try:
            stdout_json = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            pass

    return result.returncode, stdout_json, result.stderr


def _verdict_from_output(returncode: int, stdout_json: dict | None) -> str:
    """Map hook output to suppressed / surfaced / blocked.

    hedwig-decide.py nests its decision under hookSpecificOutput (the Claude
    Code PreToolUse contract). A "deny" there is the confidence-handshake /
    hard-constraint block fed back to the agent; "allow" suppresses the native
    prompt (auto-apply); no JSON = passthrough = the native prompt fires.
    """
    if stdout_json is None:
        return "surfaced"  # passthrough (no JSON output = native prompt fires)
    inner = stdout_json.get("hookSpecificOutput") or stdout_json
    decision = inner.get("permissionDecision") or inner.get("decision") or ""
    if decision == "allow":
        return "suppressed"
    if decision == "deny":
        return "blocked"
    return "surfaced"


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def _make_scenarios(demo_dir: str) -> list[dict]:
    return [
        {
            "name": "low_risk_edit",
            "description": "Small edit to an existing test file — expect auto-apply",
            "payload": _edit_payload(
                file_path=f"{demo_dir}/tests/test_store.py",
                old_string="from recipe_api.store import get_recipe, list_recipes, next_recipe_id",
                new_string="from recipe_api.store import get_recipe, list_recipes, next_recipe_id  # noqa",
                cwd=demo_dir,
            ),
            "expect": "suppressed",
        },
        {
            "name": "api_change_cold_start",
            "description": "Edit to server.py with no history — cold-start heuristic auto-applies a small general change",
            "payload": _edit_payload(
                file_path=f"{demo_dir}/server.py",
                old_string="app = Flask(__name__)",
                new_string="app = Flask(__name__, static_folder='static')",
                cwd=demo_dir,
            ),
            "expect": "suppressed",
        },
        {
            "name": "new_file_write",
            "description": "Writing a new file — expect surfaced (is_new_file risk)",
            "payload": _write_payload(
                file_path=f"{demo_dir}/recipe_api/new_module.py",
                content="# placeholder\n",
                cwd=demo_dir,
            ),
            "expect": "surfaced",
        },
        {
            "name": "security_sensitive_file",
            "description": "Edit to auth.py (security-sensitive) — confidence handshake holds it for revision",
            "payload": _edit_payload(
                file_path=f"{demo_dir}/recipe_api/auth.py",
                old_string="SECRET_KEY = 'dev'",
                new_string="SECRET_KEY = os.environ['SECRET_KEY']",
                cwd=demo_dir,
            ),
            "expect": "blocked",
        },
        {
            "name": "hard_constraint_deny",
            "description": "always_deny rule blocks an edit — expect blocked",
            "payload": _edit_payload(
                file_path=f"{demo_dir}/server.py",
                old_string="app = Flask(__name__)",
                new_string="app = Flask(__name__)",
                cwd=demo_dir,
            ),
            "expect": "blocked",
            "setup": "always_deny",  # harness seeds this constraint before running
            "deny_pattern": "server.py",  # repo-relative — what the cascade matches on
        },
        {
            "name": "non_governed_tool",
            "description": "Bash tool (not Edit/Write/MultiEdit) — expect passthrough",
            "payload": {
                "session_id": "harness-session",
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "cwd": demo_dir,
            },
            "expect": "surfaced",  # passthrough = no output = treated as surfaced
        },
    ]


# ---------------------------------------------------------------------------
# Constraint seeding for the hard_constraint_deny scenario
# ---------------------------------------------------------------------------

def _seed_always_deny(data_dir: Path, repo_root: str, file_pattern: str) -> None:
    """Write an always_deny constraint directly into trust.db."""
    sys.path.insert(0, str(PLUGIN_BIN.parent / "vendor"))
    from sc.trust_db import HardConstraint, TrustDB  # noqa: PLC0415
    db = TrustDB(data_dir / "trust.db")
    constraint = HardConstraint.for_both(
        path_pattern=file_pattern,
        constraint_type="always_deny",
        source="harness",
        overridable=False,
    )
    db.add_constraints(repo_root, [constraint])


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def _run_scenario(scenario: dict, data_dir: Path, verbose: bool) -> bool:
    name = scenario["name"]
    desc = scenario["description"]
    payload = scenario["payload"]
    expected = scenario["expect"]

    if verbose:
        print(f"\n  scenario: {name}")
        print(f"  {desc}")

    # Per-scenario setup. Constraints match on the repo-relative path, so the
    # cascade keys on the resolved repo root (repo_root_key) — mirror that here.
    if scenario.get("setup") == "always_deny":
        repo_root = str(Path(payload["cwd"]).resolve())
        pattern = scenario.get("deny_pattern") or payload["tool_input"]["file_path"]
        _seed_always_deny(data_dir, repo_root, pattern)

    rc, stdout_json, stderr = _run_hook(DECIDE_SCRIPT, payload, data_dir, verbose)
    verdict = _verdict_from_output(rc, stdout_json)

    passed = verdict == expected
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name}: got={verdict} expected={expected}")

    if not passed and stderr.strip():
        print(f"         stderr: {stderr.strip()[:200]}")

    # Fire record hook for non-blocked scenarios (mirrors real Claude Code behavior)
    if verdict != "blocked":
        _run_hook(RECORD_SCRIPT, payload, data_dir, verbose)

    return passed


def _setup_demo_copy() -> tuple[Path, str]:
    """Copy demo_recipe_api to a temp dir so the harness doesn't touch the real repo."""
    tmp = tempfile.mkdtemp(prefix="hedwig_harness_")
    demo_copy = Path(tmp) / "demo_recipe_api"
    shutil.copytree(DEMO_SRC, demo_copy)
    # Create auth.py if not present (security-sensitive scenario needs it)
    auth = demo_copy / "recipe_api" / "auth.py"
    if not auth.exists():
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text("SECRET_KEY = 'dev'\n")
    data_dir = Path(tmp) / "plugin_data"
    data_dir.mkdir()
    return demo_copy, data_dir, tmp


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Hedwig plugin end-to-end harness")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--scenario", help="Run only this scenario by name")
    args = parser.parse_args()

    demo_copy, data_dir, tmp = _setup_demo_copy()
    demo_dir = str(demo_copy)

    print(f"Hedwig plugin harness")
    print(f"  demo repo: {demo_dir}")
    print(f"  data dir:  {data_dir}")
    print()

    scenarios = _make_scenarios(demo_dir)
    if args.scenario:
        scenarios = [s for s in scenarios if s["name"] == args.scenario]
        if not scenarios:
            print(f"Unknown scenario: {args.scenario}", file=sys.stderr)
            return 1

    passed = []
    failed = []

    for scenario in scenarios:
        ok = _run_scenario(scenario, data_dir, args.verbose)
        (passed if ok else failed).append(scenario["name"])

    print()
    print(f"Results: {len(passed)} passed, {len(failed)} failed")
    if failed:
        print(f"  FAILED: {', '.join(failed)}", file=sys.stderr)

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
