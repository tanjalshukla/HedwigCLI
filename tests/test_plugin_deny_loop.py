"""R6 — deny+reason self-correction loop.

On a surfaced verdict for a GATED high-risk edit, decide.py emits
permissionDecision:"deny" + an actionable reason so the agent revises
same-turn, instead of silently passing through to the native prompt. The three
guardrails this suite locks in:

  * GATED — deny fires only for security-sensitive / high-blast / previously
    regretted edits; an ordinary surfaced edit (e.g. a brand-new file) still
    passes through to the human.
  * CAPPED — at most MAX_DENY_RETRIES denies per (session, file); the next
    surfaced decision escalates to the human (passthrough), never loops.
  * NO REGRESSION — auto-approve (proceed) edits are untouched; a handshake
    self-checkin surface is not bounced back at the agent.

Subprocess + scrubbed PYTHONPATH, like the other plugin hook tests.
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
    env["PYTHONPATH"] = ""
    env.pop("VIRTUAL_ENV", None)
    return env


def _decide(payload: dict, data_dir: Path, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=str(cwd), env=_env(data_dir),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.returncode, proc.stdout


def _edit_payload(proj: Path, rel: str, *, session="s1", old="x", new="y") -> dict:
    return {
        "tool_name": "Edit",
        "cwd": str(proj),
        "session_id": session,
        "tool_input": {"file_path": str(proj / rel), "old_string": old, "new_string": new},
    }


def _write_payload(proj: Path, rel: str, *, session="s1", content="x = 1\n") -> dict:
    return {
        "tool_name": "Write",
        "cwd": str(proj),
        "session_id": session,
        "tool_input": {"file_path": str(proj / rel), "content": content},
    }


def _verdict(stdout: str) -> str | None:
    if not stdout:
        return None  # passthrough
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecision"]


def test_security_sensitive_edit_is_denied_with_reason(tmp_path: Path) -> None:
    """A security-sensitive surfaced edit trips the gate → deny + actionable
    reason (not a silent passthrough)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "auth.py").write_text("TOKEN = 'old'\n")
    _, out = _decide(
        _edit_payload(proj, "auth.py", old="'old'", new="'rotated'"),
        tmp_path / "data", proj,
    )
    assert _verdict(out) == "deny", f"expected deny, got {out!r}"
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Hedwig is holding this edit" in reason
    assert "re-propose" in reason or "narrow" in reason.lower()


def test_ordinary_new_file_passes_through_not_denied(tmp_path: Path) -> None:
    """A brand-new non-risky file surfaces (new-file penalty) but must NOT be
    denied — first-sight files go to the human; deny is for revisable risk."""
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    _, out = _decide(
        _write_payload(proj, "src/new_module.py", content="def f():\n    return 1\n"),
        tmp_path / "data", proj,
    )
    assert out == "", f"expected passthrough (no deny) for an ordinary new file, got {out!r}"


def test_low_risk_edit_still_auto_approves(tmp_path: Path) -> None:
    """No regression: R6 must not touch the auto-approve path."""
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_x.py").write_text("def test():\n    assert 1 == 1\n")
    _, out = _decide(
        _edit_payload(proj, "tests/test_x.py", old="1 == 1", new="2 == 2"),
        tmp_path / "data", proj,
    )
    assert _verdict(out) == "allow", f"low-risk edit must still auto-apply, got {out!r}"


def test_deny_is_retry_capped_then_escalates(tmp_path: Path) -> None:
    """After MAX_DENY_RETRIES denies on the same (session, file), the next
    surfaced decision escalates to the human (passthrough), never loops."""
    from importlib import import_module
    import sys
    sys.path.insert(0, str(_PLUGIN_BIN))
    cap = import_module("_hedwig_common").MAX_DENY_RETRIES
    del sys.path[0]

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "auth.py").write_text("TOKEN = 'v0'\n")
    data_dir = tmp_path / "data"

    # The first `cap` attempts are denied...
    for i in range(cap):
        _, out = _decide(
            _edit_payload(proj, "auth.py", old=f"'v{i}'", new=f"'v{i+1}'"),
            data_dir, proj,
        )
        assert _verdict(out) == "deny", f"attempt {i} should deny, got {out!r}"

    # ...the next one escalates to the human instead of denying again.
    _, out = _decide(
        _edit_payload(proj, "auth.py", old="'vX'", new="'vY'"),
        data_dir, proj,
    )
    assert out == "", f"after the cap, must escalate to human (passthrough), got {out!r}"


def test_prior_regret_gates_deny_on_otherwise_ordinary_file(tmp_path: Path) -> None:
    """A file with a recorded denial in its history trips the gate even if the
    edit itself isn't security-sensitive — the 'you regretted this before' arm."""
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "util.py").write_text("def helper():\n    return 1\n")
    data_dir = tmp_path / "data"

    # Seed a denial in this file's history via the trust.db (as the verify/record
    # hooks would on a regret).
    # Run a tiny helper subprocess to write the trace with the vendored sc.
    seed = subprocess.run(
        ["python3", "-c",
         "import sys; sys.path.insert(0, r'%s')\n"
         "from _hedwig_common import open_trust_db\n"
         "db = open_trust_db()\n"
         "db.record_trace(repo_root=r'%s', session_id='s0', task=r'%s', stage='apply',\n"
         "  action_type='file_update', file_path='src/util.py', change_type=None,\n"
         "  diff_size=None, blast_radius=None, existing_lease=False, lease_type=None,\n"
         "  prior_approvals=0, prior_denials=0, policy_action='check_in', policy_score=0.0,\n"
         "  user_decision='deny', verification_passed=False)\n"
         % (str(_PLUGIN_BIN), str(proj), str(proj))],
        capture_output=True, text=True, env=_env(data_dir),
    )
    assert seed.returncode == 0, seed.stderr

    _, out = _decide(
        _edit_payload(proj, "src/util.py", old="return 1", new="return 2"),
        data_dir, proj,
    )
    assert _verdict(out) == "deny", f"a previously-regretted file should deny, got {out!r}"
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "reverted or failed a check" in reason


def test_learned_scorer_cannot_autoapply_security_file(tmp_path: Path) -> None:
    """Invariant 5: the deterministic security floor must hold even when the
    learned classifier has been trained to 'approve everything'. A classifier
    drifted toward proceed (as a busy booth produces) would return proceed for a
    security-sensitive file and auto-apply it before the surfaced-branch gate
    ever runs — so decide.py floors is_security_sensitive to a surface, which
    then escalates to deny. Regression guard for the BUG#3 bypass."""
    proj = tmp_path / "proj"
    (proj / "recipe_api").mkdir(parents=True)
    (proj / "recipe_api" / "auth.py").write_text(
        "def check_api_key(req):\n    return req.headers.get('X-API-Key') == 's'\n"
    )
    data_dir = tmp_path / "data"

    # Train the per-repo classifier to approve everything (12 low-risk approvals,
    # past MIN_SAMPLES_FOR_LEARNED) and force the learned path on (HEDWIG_PYTHON
    # = this interpreter, which has numpy/sklearn in the test env).
    seed = subprocess.run(
        ["python3", "-c",
         "import sys; sys.path.insert(0, r'%s')\n"
         "from _hedwig_common import open_trust_db\n"
         "from sc.ml_policy import build_cold_classifier\n"
         "from sc.policy import PolicyInput\n"
         "db = open_trust_db()\n"
         "clf = build_cold_classifier()\n"
         "pi = PolicyInput(prior_approvals=5, prior_denials=0, avg_response_ms=8000,\n"
         "  avg_edit_distance=0.1, diff_size=8, blast_radius=1, is_new_file=False,\n"
         "  is_security_sensitive=False, change_pattern='general_change',\n"
         "  recent_denials=0, files_in_action=1)\n"
         "[clf.update(pi, approved=True) for _ in range(12)]\n"
         "db.save_policy_model(r'%s', clf)\n"
         "assert clf.ready()\n"
         % (str(_PLUGIN_BIN), str(proj))],
        capture_output=True, text=True, env=_env(data_dir),
    )
    if seed.returncode != 0:
        import pytest
        # No numpy/sklearn here → the learned path can't be exercised; the
        # heuristic already floors security. Skip rather than false-pass.
        pytest.skip(f"learned path unavailable: {seed.stderr.strip().splitlines()[-1:]}")

    env = _env(data_dir)
    env["HEDWIG_PYTHON"] = "python3"  # force re-exec onto a deps-capable interp
    proc = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=json.dumps(_edit_payload(proj, "recipe_api/auth.py", old="'s'", new="'s2'")),
        capture_output=True, text=True, cwd=str(proj), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    # The classifier wants to proceed (score ~1.0); the security floor must stop
    # it auto-applying. Acceptable outcomes: deny (gate escalation) or a
    # passthrough surface — NEVER "allow".
    assert _verdict(proc.stdout) != "allow", (
        f"learned scorer auto-applied a security-sensitive file — floor bypassed: {proc.stdout!r}"
    )
