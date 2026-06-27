"""The memory-layer injector (Part B) — hedwig-context.py.

The plugin can't touch Claude Code's system prompt, so it delivers Hedwig's
"what we've learned about this repo" memory through the SessionStart /
UserPromptSubmit hooks' `additionalContext` (which the MODEL reads). These
tests seed a repo's trust.db, run the hook as Claude Code would (JSON on stdin),
and assert the synthesized context comes back in the hookSpecificOutput.

The synthesis reuses sc.repo_memory.synthesize_repo_summary — the same function
the CLI builds its system-prompt lead from — so the two front-ends can't drift.
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
    return env


def _run_context(event: str, payload: dict, data_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-context.py"), event],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(data_dir),
    )


def _seed_db(data_dir: Path, repo: str) -> None:
    """Seed a confirmed preference + logic note + guideline + feedback trace."""
    from sc.trust_db import TrustDB

    data_dir.mkdir(parents=True, exist_ok=True)
    db = TrustDB(data_dir / "trust.db")
    # A confirmed preference (drives the repo-summary "Confirmed preferences:" line).
    db.save_confirmed_preference(
        repo_root=repo,
        session_id="seed",
        preference_json=json.dumps({"accepted": True, "driver": "scope_constraint"}),
        driver="scope_constraint",
    )
    # A repo fact (logic note) + a task-relevant guideline.
    db.add_logic_notes(
        repo, source="seed", notes=["tests live in tests/, not test/"], files=["tests/"]
    )
    db.add_behavioral_guidelines(
        repo, source="seed",
        guidelines=["Use dependency injection for service classes in the API layer"],
    )
    # A feedback snippet (recorded as a trace with user_feedback_text).
    db.record_trace(
        repo_root=repo, session_id="seed", task="seed", stage="apply",
        action_type="file_update", file_path="api/routes.py",
        change_type="api_change", diff_size=10, blast_radius=1,
        existing_lease=False, lease_type=None, prior_approvals=0, prior_denials=0,
        policy_action="check_in", policy_score=0.0, user_decision="deny",
        user_feedback_text="prefer composition over inheritance here",
    )


def test_session_start_injects_repo_summary(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo = str(tmp_path / "repo")
    _seed_db(data_dir, repo)

    out = _run_context("SessionStart", {"cwd": repo, "source": "startup"}, data_dir)
    assert out.returncode == 0, out.stderr
    assert out.stdout, "SessionStart with seeded memory must inject context"
    obj = json.loads(out.stdout)["hookSpecificOutput"]
    assert obj["hookEventName"] == "SessionStart"
    ctx = obj["additionalContext"]
    assert "what we've learned about this repo" in ctx.lower()
    # The confirmed preference + repo fact must surface in the paragraph.
    assert "Confirmed preferences" in ctx
    assert "tests live in tests/" in ctx


def test_user_prompt_submit_injects_relevant_guidelines(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo = str(tmp_path / "repo")
    _seed_db(data_dir, repo)

    # A prompt overlapping the guideline's keywords ("service", "API").
    out = _run_context(
        "UserPromptSubmit",
        {"cwd": repo, "prompt": "refactor the API service class to be testable"},
        data_dir,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout, "a prompt overlapping a guideline must inject it"
    obj = json.loads(out.stdout)["hookSpecificOutput"]
    assert obj["hookEventName"] == "UserPromptSubmit"
    assert "dependency injection" in obj["additionalContext"]


def test_empty_db_injects_nothing(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo = str(tmp_path / "repo")
    (data_dir).mkdir(parents=True, exist_ok=True)

    out = _run_context("SessionStart", {"cwd": repo, "source": "startup"}, data_dir)
    assert out.returncode == 0, out.stderr
    assert out.stdout == "", "no memory → no empty system reminder injected"


def test_missing_cwd_is_safe(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    out = _run_context("SessionStart", {"source": "startup"}, data_dir)
    assert out.returncode == 0
    assert out.stdout == ""


def test_non_dict_payload_is_safe(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proc = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-context.py"), "SessionStart"],
        input="[1, 2, 3]", capture_output=True, text=True, env=_env(data_dir),
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert "Traceback" not in proc.stderr


def test_context_hook_disables_embeddings(tmp_path: Path) -> None:
    """Latency guard: the per-prompt hook must force keyword retrieval, never
    materialize the fastembed model (~5s on a cold subprocess, and every hook is
    a cold subprocess). The hook sets HEDWIG_DISABLE_EMBEDDINGS at import; here
    we confirm (a) select_ranker honors the env var, in an isolated subprocess
    so no module state leaks, and (b) importing the hook sets it."""
    probe = (
        "import os; os.environ['HEDWIG_DISABLE_EMBEDDINGS']='1';"
        "import sys; sys.path.insert(0, %r);"
        "from sc.retrieval import select_ranker;"
        "print(select_ranker()[1])" % str(_VENDOR)
    )
    out = subprocess.run(["python3", "-c", probe], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "keyword", "env var must force keyword ranking"

    # And the hook itself sets the var at import time.
    hook_src = (_PLUGIN_BIN / "hedwig-context.py").read_text()
    assert "HEDWIG_DISABLE_EMBEDDINGS" in hook_src, (
        "hedwig-context must opt out of embeddings on the hot path"
    )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
