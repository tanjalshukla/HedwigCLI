#!/usr/bin/env python3
"""Hedwig PostToolUse adapter — records executed governed actions to trust.db.

When a governed Edit/Write/MultiEdit actually executes, this hook persists a
decision trace so the next decide call for the same file sees real outcome
history (via TrustDB.policy_history → HeuristicScorer). This is the positive
half of the outcome loop: an auto-applied action that executed and (so far)
survived is provisional positive history. The Stop hook supplies the negative
half — reversal / verification failure recorded as a negative-outcome trace.

Constraint (verified): PostToolUse cannot distinguish auto-applied-by-our-hook
from approved-by-user-at-native-prompt. We correlate with the verdict
hedwig-decide.py logged to decisions.jsonl for the same (session_id,
file_path): a "suppressed" verdict → this executed because WE auto-allowed it
→ record as auto_approve. Otherwise the user approved a surfaced action at the
native prompt → record as approve (still positive outcome history, but not
attributable to Hedwig's auto-apply).

ZERO-DEP: only calls TrustDB.record_trace / record_decision (pure stdlib).
Never materializes a PolicyClassifier. Always exits 0; a recording failure
must never break the session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _hedwig_common import (  # noqa: E402
    DECISIONS_LOG,
    _iter_jsonl,
    append_jsonl,

    ensure_learned_interpreter,
    open_trust_db,
    policy_input_for_decision,
    policy_input_for_regret,
    repo_root_key,
    update_classifier_for_decision,
    update_classifier_for_regret,
)

_GOVERNED = {"Edit", "Write", "MultiEdit"}


def _last_verdict(session_id: str | None, file_path: str | None) -> dict | None:
    """Most recent decide verdict for this (session_id, file_path), or None."""
    for row in _iter_jsonl(DECISIONS_LOG, reverse=True):
        if row.get("session_id") == session_id and row.get("file_path") == file_path:
            return row
    return None


def _is_reversal(session_id: str | None, file_path: str | None, cur_old: str, cur_new: str) -> bool:
    """True if this Edit undoes a prior auto-applied edit on the same file.

    The verification-independent negative signal (R1): Hedwig auto-applied an
    edit A→B on this file; the agent now edits B→A, putting it back. That
    reversal is a regret with NO dependency on a configured verify command —
    the most demoable outcome signal ("I auto-approved, the agent undid it, I
    got warier"). We require an exact inverse against a *suppressed* prior
    edit so a routine follow-up edit isn't mistaken for a revert. Empty
    strings never count (a no-op pair can't be a meaningful reversal).
    """
    if not (cur_old or cur_new):
        return False
    # Use _iter_jsonl (sibling-dir-aware) so reversal detection works even when
    # the hook and the recorder receive different CLAUDE_PLUGIN_DATA values.
    for row in _iter_jsonl(DECISIONS_LOG, reverse=True):
        if row.get("session_id") != session_id or row.get("file_path") != file_path:
            continue
        if row.get("verdict") != "suppressed":
            continue
        prior_old = row.get("edit_old") or ""
        prior_new = row.get("edit_new") or ""
        if not (prior_old or prior_new):
            continue
        # Exact inverse: the prior auto-applied edit was prior_old -> prior_new,
        # and this edit is prior_new -> prior_old.
        if cur_old == prior_new and cur_new == prior_old:
            return True
    return False


def _record_reversal_regret(repo_root: str, session_id: str, rel: str, change_pattern: str | None) -> None:
    """Persist a reversal as a negative-outcome trace, feed it to the
    classifier as a corrective gradient, and mark the demoable regret event.

    `repo_root` is the canonical (resolved) repo key, matching every other
    trust.db access. Two learning channels, both the CAIS mechanism (S5):
      * a user_decision='deny' apply trace → the next decide on THIS file
        tightens via per-file history (the -0.7 denial weight / learned score);
      * classifier.update(approved=False, count_sample=False) → the negative
        gradient also generalizes to risk-signal-similar edits on OTHER files
        (what the online log-reg buys over the pure heuristic).
    regret.jsonl records the event for /hedwig-status. Best-effort; never
    raises into the hook.
    """
    append_jsonl(
        "regret.jsonl",
        {
            "session_id": session_id,
            "cwd": repo_root,
            "files": [rel],
            "signal": "reversal",  # distinguishes from verification-failure regret
        },
    )
    try:
        db = open_trust_db()
        db.record_trace(
            repo_root=repo_root,
            session_id=session_id,
            task=repo_root,
            stage="apply",
            action_type="file_update",
            file_path=rel,
            change_type=change_pattern,
            diff_size=None,
            blast_radius=None,
            existing_lease=False,
            lease_type=None,
            prior_approvals=0,
            prior_denials=0,
            policy_action="check_in",
            policy_score=0.0,
            user_decision="deny",  # negative outcome: agent reverted the auto-applied edit
            user_feedback_text="agent reverted an auto-applied edit",
            verification_passed=False,
        )
        # Corrective classifier gradient — keyed so it fires exactly once.
        pi = policy_input_for_regret(db, repo_root, session_id, rel)
        update_classifier_for_regret(db, repo_root, pi, regret_key=f"reversal:{session_id}:{rel}")
    except Exception:
        pass


def _rel_path(cwd: str, file_path: str) -> str:
    try:
        target = Path(file_path)
        if target.is_absolute():
            return str(target.relative_to(Path(cwd)))
    except ValueError:
        pass
    return file_path


def _run_hypothesis_evidence(db, repo_root: str, session_id: str) -> None:
    """Seed rule-based hypothesis candidates and accumulate evidence.

    The plugin's evidence side of the hypothesis bank, mirroring
    apply_stage._run_hypothesis_pipeline but local-only: read this session's
    traces back, build the SessionSummary + pushback counts the generators
    expect, seed any candidates whose signals fit, and score the latest trace
    against pending candidates. No Bedrock — the LLM noticer is a separate
    opt-in path. Candidates accumulate silently; nothing surfaces or affects
    behavior here (the /hedwig-learn command surfaces ready ones for
    confirmation). Best-effort: any failure is swallowed so recording never
    breaks.
    """
    try:
        from sc.hypothesis_bank import (  # noqa: PLC0415
            seed_candidates_from_session,
            update_evidence,
        )
        from sc.preference_inference import (  # noqa: PLC0415
            infer_user_persona,
            pushback_counts_from_rows,
            summarize_session,
        )

        rows = db.session_traces(repo_root, session_id)
        if not rows:
            return
        summary = summarize_session(rows)
        pushback_counts = pushback_counts_from_rows(rows)
        persona = infer_user_persona(summary)
        seed_candidates_from_session(
            trust_db=db,
            repo_root=repo_root,
            session_id=session_id,
            session_summary=summary,
            pushback_counts=pushback_counts,
            inferred_persona=persona,
        )
        # Score the most recent trace against all pending candidates.
        last = rows[-1]
        update_evidence(
            trust_db=db,
            repo_root=repo_root,
            session_id=session_id,
            trace=dict(last),
        )
    except Exception:
        pass


def main() -> int:
    """Top-level guard — a PostToolUse hook must never exit non-zero. Any
    unanticipated payload shape or internal error is swallowed (exit 0); the
    edit already ran, recording is best-effort."""
    try:
        return _main_inner()
    except Exception:
        return 0


def _main_inner() -> int:
    # Re-exec under a deps-capable interpreter before reading stdin, so the
    # regret classifier update (update_classifier_for_regret) runs the real
    # learned path at the booth rather than silently degrading.
    ensure_learned_interpreter()

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0  # valid JSON but not an object (list/str/num)

    tool_name = payload.get("tool_name") or ""
    if tool_name not in _GOVERNED:
        return 0

    # Defend against a non-dict tool_input or non-string file_path (valid JSON,
    # unexpected shape) — would otherwise crash on .get and exit non-zero.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    abs_file = tool_input.get("file_path")
    if not isinstance(abs_file, str) or not abs_file:
        return 0

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    rel = _rel_path(cwd, abs_file)
    # DB key, canonicalized so it matches what the slash commands and decide
    # hook derive (cwd stays raw for the _rel_path math above).
    repo_root = repo_root_key(cwd)

    # decide.py logs the verdict keyed by the repo-relative path, so correlate
    # on `rel` (not the absolute path) — otherwise every lookup misses and
    # auto-applied actions get mis-tagged as plain user approvals.
    verdict_row = _last_verdict(session_id, rel)
    suppressed = bool(verdict_row and verdict_row.get("verdict") == "suppressed")
    change_pattern = (verdict_row or {}).get("change_pattern")

    # R1: verification-independent regret. If this Edit undoes a prior
    # auto-applied edit on the same file, the agent reverted Hedwig's action —
    # record it as a negative outcome so the next decide tightens, and stop.
    # A reversal is NOT a fresh positive edit, so we do not also record an
    # approve/auto_approve trace for it.
    if tool_name == "Edit":
        cur_old = tool_input.get("old_string") or ""
        cur_new = tool_input.get("new_string") or ""
        if _is_reversal(session_id, rel, cur_old, cur_new):
            append_jsonl(
                "traces.jsonl",
                {
                    "session_id": session_id,
                    "cwd": cwd,
                    "tool_name": tool_name,
                    "file_path": rel,
                    "user_decision": "deny",
                    "signal": "reversal",
                },
            )
            _record_reversal_regret(repo_root, session_id, rel, change_pattern)
            return 0
    # Suppressed → Hedwig auto-applied it. Surfaced+executed → user approved at
    # the native prompt. Both are positive outcome history; the tag preserves
    # attribution for /hedwig-status and later analysis.
    user_decision = "auto_approve" if suppressed else "approve"
    score = (verdict_row or {}).get("score", 0.0)

    # Keep the JSONL trace too (cheap, human-readable, survives DB issues).
    append_jsonl(
        "traces.jsonl",
        {
            "session_id": session_id,
            "cwd": cwd,
            "tool_name": tool_name,
            "file_path": rel,
            "user_decision": user_decision,
        },
    )

    try:
        db = open_trust_db()
        db.record_trace(
            repo_root=repo_root,
            session_id=session_id,
            task=repo_root,  # plugin has no task string; key history per repo
            stage="apply",
            action_type="file_update",
            file_path=rel,
            change_type=change_pattern,
            diff_size=None,
            blast_radius=None,
            existing_lease=False,
            lease_type=None,
            prior_approvals=0,
            prior_denials=0,
            policy_action="proceed" if suppressed else "check_in",
            policy_score=float(score) if score is not None else 0.0,
            user_decision=user_decision,
        )
        # Positive learning sample. An executed governed edit — auto-applied or
        # approved at the native prompt — is positive outcome history, so replay
        # it as classifier.update(approved=True). This is the ONLY place
        # sample_count grows on the plugin path; without it the online scorer
        # never reaches ready() and the learned scorer never takes over. The
        # PolicyInput is rebuilt from the RiskSignals decide.py logged, so we
        # learn on the exact features that decision was scored on. Skipped when
        # decide left no risk signals (e.g. cold log row from before this fix).
        if verdict_row and verdict_row.get("blast_radius") is not None:
            pi = policy_input_for_decision(db, repo_root, rel, verdict_row)
            update_classifier_for_decision(db, repo_root, pi, approved=True)
        # Hypothesis bank: seed rule-based candidates from this session's traces
        # and accumulate evidence on the latest one. Pure-local (no Bedrock) —
        # the LLM noticer is a separate opt-in path. Candidates never affect
        # behavior until the developer confirms one via /hedwig-learn.
        _run_hypothesis_evidence(db, repo_root, session_id)
    except Exception:
        # DB write is best-effort; the JSONL trace above is the fallback.
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
