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


def _is_reversal(
    session_id: str | None,
    file_path: str | None,
    cur_old: str,
    cur_new: str,
    structured_patch: list | None = None,
) -> bool:
    """True if this Edit undoes a prior auto-applied edit on the same file.

    Two detection strategies, most robust first:

    1. Structured-patch analysis (when PostToolUse provides structuredPatch):
       A patch that removes more lines than it adds on a file Hedwig suppressed
       this session is likely a partial or full reversal. This catches reversals
       that don't exactly invert the old/new strings (e.g. partial undos, merged
       changes). We use this as the primary signal when available.

    2. Exact-inverse fallback (original detection): the prior edit was
       old→new; this edit is new→old. Precise but brittle — misses partial undos.

    Both require a prior *suppressed* verdict on this (session, file). Empty
    strings and empty patches never count as reversals.
    """
    if not (cur_old or cur_new):
        return False

    # Strategy 1: structured patch — net-deletion on a file we auto-applied.
    if structured_patch and isinstance(structured_patch, list):
        for hunk in structured_patch:
            if not isinstance(hunk, dict):
                continue
            lines = hunk.get("lines") or []
            removed = sum(1 for l in lines if isinstance(l, str) and l.startswith("-"))
            added = sum(1 for l in lines if isinstance(l, str) and l.startswith("+"))
            if removed > added:
                # Net deletion on this file. Check if Hedwig suppressed it recently.
                for row in _iter_jsonl(DECISIONS_LOG, reverse=True):
                    if row.get("session_id") == session_id and row.get("file_path") == file_path:
                        if row.get("verdict") == "suppressed":
                            return True
                        break  # most recent verdict was not suppressed — not a reversal
                break

    # Strategy 2: exact string inverse.
    for row in _iter_jsonl(DECISIONS_LOG, reverse=True):
        if row.get("session_id") != session_id or row.get("file_path") != file_path:
            continue
        if row.get("verdict") != "suppressed":
            continue
        prior_old = row.get("edit_old") or ""
        prior_new = row.get("edit_new") or ""
        if not (prior_old or prior_new):
            continue
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
        # Build PolicyInput BEFORE record_trace so history reflects the state
        # at decision time — same ordering fix as the positive-sample path.
        pi = policy_input_for_regret(db, repo_root, session_id, rel)
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
            user_decision="deny",
            user_feedback_text="agent reverted an auto-applied edit",
            verification_passed=False,
        )
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

    import time as _time  # noqa: PLC0415

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or ""
    rel = _rel_path(cwd, abs_file)
    repo_root = repo_root_key(cwd)

    # Structured patch from tool_response (PostToolUse only).
    tool_response = payload.get("tool_response") or {}
    structured_patch = tool_response.get("structuredPatch") if isinstance(tool_response, dict) else None

    verdict_row = _last_verdict(session_id, rel)
    suppressed = bool(verdict_row and verdict_row.get("verdict") == "suppressed")
    surfaced = bool(verdict_row and verdict_row.get("verdict") == "surfaced")
    change_pattern = (verdict_row or {}).get("change_pattern")

    # Response time: time between Hedwig's PreToolUse decision and this PostToolUse.
    # Only meaningful for surfaced edits (the developer had to actively approve);
    # for suppressed edits it's just tool execution latency.
    response_time_ms: int | None = None
    if verdict_row and verdict_row.get("pre_tool_ts"):
        try:
            elapsed_ms = int((_time.time() - float(verdict_row["pre_tool_ts"])) * 1000)
            if 0 < elapsed_ms < 600_000:  # sanity: 0–10 min
                response_time_ms = elapsed_ms
        except Exception:
            pass

    # Rubber stamp: surfaced edit approved in under 5 seconds — developer didn't
    # really review it. Weight this approval at 0.5 in the classifier.
    rubber_stamp = False
    if surfaced and response_time_ms is not None:
        rubber_stamp = response_time_ms < 5_000

    # R1: verification-independent regret. If this Edit undoes a prior
    # auto-applied edit on the same file, record it as negative and stop.
    if tool_name == "Edit":
        cur_old = tool_input.get("old_string") or ""
        cur_new = tool_input.get("new_string") or ""
        if _is_reversal(session_id, rel, cur_old, cur_new, structured_patch):
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

    # Suppressed → Hedwig auto-applied. Surfaced+executed → developer approved
    # at the native prompt. Both are positive outcome history.
    user_decision = "auto_approve" if suppressed else "approve"
    score = (verdict_row or {}).get("score", 0.0)
    effort_level = (verdict_row or {}).get("effort_level") or ""
    is_security_sensitive = bool((verdict_row or {}).get("is_security_sensitive"))

    append_jsonl(
        "traces.jsonl",
        {
            "session_id": session_id,
            "cwd": cwd,
            "tool_name": tool_name,
            "file_path": rel,
            "user_decision": user_decision,
            "response_time_ms": response_time_ms,
            "rubber_stamp": rubber_stamp,
            "effort_level": effort_level,
        },
    )

    try:
        db = open_trust_db()
        # Build PolicyInput BEFORE record_trace so prior_approvals/denials in
        # policy_history reflect the state at decision time, not after we write
        # this new trace. Writing the trace first would inflate prior_approvals
        # by one for every positive sample, biasing the classifier.
        pi = None
        if verdict_row and verdict_row.get("blast_radius") is not None:
            pi = policy_input_for_decision(db, repo_root, rel, verdict_row)
        db.record_trace(
            repo_root=repo_root,
            session_id=session_id,
            task=repo_root,
            stage="apply",
            action_type="file_update",
            file_path=rel,
            change_type=change_pattern,
            diff_size=int((verdict_row or {}).get("diff_size") or 0) or None,
            blast_radius=int((verdict_row or {}).get("blast_radius") or 0) or None,
            existing_lease=False,
            lease_type=None,
            prior_approvals=0,
            prior_denials=0,
            policy_action="proceed" if suppressed else "check_in",
            policy_score=float(score) if score is not None else 0.0,
            user_decision=user_decision,
            response_time_ms=response_time_ms,
            rubber_stamp=rubber_stamp,
            is_security_sensitive=is_security_sensitive,
        )
        if pi is not None:
            update_classifier_for_decision(
                db, repo_root, pi, approved=True, rubber_stamp=rubber_stamp
            )
        _run_hypothesis_evidence(db, repo_root, session_id)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
