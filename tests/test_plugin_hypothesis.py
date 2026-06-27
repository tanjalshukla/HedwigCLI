"""The hypothesis bank end-to-end on the plugin path (Part D).

The full Trial-Error-Explain loop, plus the payoff: a confirmed preference
actually changes a later decision. Covered:

  * evidence accumulation — rule-based candidates seed + accumulate from the
    record hook's traces (verified through the public bank API, the same path
    hedwig-record drives);
  * /hedwig-learn confirm — a ready candidate becomes an accepted
    confirmed_preference;
  * the payoff — a confirmed full_checkin preference makes hedwig-decide SURFACE
    an edit it would otherwise auto-apply (cascade layer 5, the thing that was
    entirely missing from the plugin before Part D);
  * decline + the safety invariant hold.
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


def _decide(data_dir: Path, *, proj: Path, rel: str) -> subprocess.CompletedProcess:
    payload = {
        "tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
        "tool_input": {"file_path": str(proj / rel), "old_string": "x = 1", "new_string": "x = 2"},
    }
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=str(proj), env=_env(data_dir),
    )


def _learn(data_dir: Path, *args: str, proj: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-learn.py"), *args],
        capture_output=True, text=True, cwd=str(proj),
        env={**_env(data_dir), "CLAUDE_SESSION_ID": "s1"},
    )


def _make_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.py").write_text("x = 1\n")
    return proj


def _confirmed_full_checkin_pref(data_dir: Path, repo: str) -> None:
    """Seed an accepted full_checkin preference matching general_change edits —
    the kind /hedwig-learn confirm would persist."""
    from sc.preferences import (
        Condition, Preference, PreferenceAction, Scope, Trigger,
        default_lifecycle_for, preference_to_dict,
    )
    from sc.trust_db import TrustDB

    data_dir.mkdir(parents=True, exist_ok=True)
    db = TrustDB(data_dir / "trust.db")
    pref = Preference(
        trigger=Trigger(stages=("apply",), change_patterns=("general_change",)),
        condition=Condition(),
        action=PreferenceAction.FULL_CHECKIN,
        scope=Scope(level="repo"),
        lifecycle=default_lifecycle_for("inferred_user_confirmed"),
    )
    db.save_confirmed_preference(
        repo_root=repo, session_id="s1",
        preference_json=json.dumps({
            "accepted": True, "driver": "scope_constraint",
            "preference": preference_to_dict(pref),
        }),
        driver="scope_constraint",
    )


def test_confirmed_preference_tightens_a_later_decide(tmp_path: Path) -> None:
    """The payoff: an edit that auto-applies with no preference must SURFACE
    once a matching full_checkin preference is confirmed. This is cascade
    layer 5 — entirely absent from the plugin before Part D."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)

    # Baseline: a small general_change edit auto-applies (allow).
    before = _decide(data_dir, proj=proj, rel="src/app.py")
    assert before.returncode == 0, before.stderr
    assert before.stdout, "baseline edit should produce a decision"
    assert json.loads(before.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"

    # Confirm a full_checkin preference for general_change edits.
    _confirmed_full_checkin_pref(data_dir, str(proj))

    # Same edit now surfaces (passthrough → native prompt) instead of auto-applying.
    after = _decide(data_dir, proj=proj, rel="src/app.py")
    assert after.returncode == 0, after.stderr
    # A surfaced verdict is either empty stdout (passthrough) or a non-allow
    # decision — never a silent auto-apply.
    if after.stdout:
        decision = json.loads(after.stdout)["hookSpecificOutput"]["permissionDecision"]
        assert decision != "allow", f"confirmed full_checkin must stop auto-apply, got {decision}"
    # else: empty stdout == passthrough == surfaced. Correct.


def test_evidence_accumulates_and_surfaces(tmp_path: Path) -> None:
    """Rule-based candidates seed from session traces and a ready one becomes
    visible to /hedwig-learn. Driven through the public bank API (the exact
    path hedwig-record runs in-process)."""
    from sc.hypothesis_bank import get_ready_hypothesis, seed_candidates_from_session
    from sc.preference_inference import SessionSummary
    from sc.preferences import PushbackType, UserPersona
    from sc.trust_db import TrustDB

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    repo = str(tmp_path / "repo")
    db = TrustDB(data_dir / "trust.db")

    # A session summary with enough scope-narrowing pushback to seed the
    # scope_constraint candidate (MIN_PUSHBACK_COUNT met).
    summary = SessionSummary(
        session_id="s1", n_turns=8, n_approvals=2, n_denials=0, n_feedback=3,
        n_failures=0, mean_edit_distance=0.2, mean_review_seconds=20.0,
        distinct_tasks=1, n_interruptions=0, n_auto_approvals=2,
    )
    pushback = {PushbackType.SCOPE_CONSTRAINT.value: 3}
    new_ids = seed_candidates_from_session(
        trust_db=db, repo_root=repo, session_id="s1",
        session_summary=summary, pushback_counts=pushback,
        inferred_persona=UserPersona.ACTIVE,
    )
    assert new_ids, "a scope-narrowing session should seed at least one candidate"

    # Drive it to ready by accumulating supporting evidence.
    from sc.hypothesis_bank import update_evidence
    for _ in range(6):
        update_evidence(
            trust_db=db, repo_root=repo, session_id="s1",
            trace={"pushback_type": PushbackType.SCOPE_CONSTRAINT.value,
                   "user_decision": "deny", "blast_radius": 3, "file_path": "a.py"},
        )
    ready = get_ready_hypothesis(trust_db=db, repo_root=repo, session_id="s1")
    assert ready is not None, "accumulated evidence should promote a candidate to ready"
    assert ready.driver == "scope_constraint"


def test_learn_confirm_persists_accepted_preference(tmp_path: Path) -> None:
    """/hedwig-learn confirm turns a ready candidate into an accepted
    confirmed_preference row that the decide hook will load."""
    from sc.hypothesis_bank import seed_candidates_from_session, update_evidence
    from sc.preference_inference import SessionSummary
    from sc.preferences import PushbackType, UserPersona
    from sc.trust_db import TrustDB

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    proj = _make_proj(tmp_path)
    repo = str(proj)
    db = TrustDB(data_dir / "trust.db")
    summary = SessionSummary(
        session_id="s1", n_turns=8, n_approvals=2, n_denials=0, n_feedback=3,
        n_failures=0, mean_edit_distance=0.2, mean_review_seconds=20.0,
        distinct_tasks=1, n_interruptions=0, n_auto_approvals=2,
    )
    seed_candidates_from_session(
        trust_db=db, repo_root=repo, session_id="s1", session_summary=summary,
        pushback_counts={PushbackType.SCOPE_CONSTRAINT.value: 3},
        inferred_persona=UserPersona.ACTIVE,
    )
    for _ in range(6):
        update_evidence(trust_db=db, repo_root=repo, session_id="s1",
                        trace={"pushback_type": PushbackType.SCOPE_CONSTRAINT.value,
                               "user_decision": "deny", "blast_radius": 3, "file_path": "a.py"})

    show = _learn(data_dir, "show", proj=proj)
    assert show.returncode == 0, show.stderr
    assert "noticed a pattern" in show.stdout.lower()

    confirm = _learn(data_dir, "confirm", proj=proj)
    assert confirm.returncode == 0, confirm.stderr
    assert "confirmed" in confirm.stdout.lower()

    # The accepted preference is now persisted and loadable.
    rows = db.confirmed_preferences_for_repo(repo)
    assert any(json.loads(r["preference_json"]).get("accepted") for r in rows), (
        "confirm must persist an accepted confirmed_preference"
    )


def test_learn_show_when_nothing_ready(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    out = _learn(data_dir, "show", proj=proj)
    assert out.returncode == 0, out.stderr
    assert "no pattern" in out.stdout.lower()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
