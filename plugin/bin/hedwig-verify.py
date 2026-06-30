#!/usr/bin/env python3
"""Hedwig Stop adapter — the negative half of the outcome loop.

At end of turn, if a verification command is configured, run it. On failure,
record a negative-outcome trace for the files auto-applied this session that
are ALSO part of the failing change (working-tree git diff) — not every file
touched this session. Scoping blame to the diff is the R1 fix: an unrelated
clean file must not tighten because some other edit broke the build. When the
change set can't be determined (no git / git error) we record nothing rather
than over-attribute. The next decide for a tainted file then sees the denial
in its history and the heuristic scorer tightens (the -0.7 denial weight in
policy.py outweighs prior auto-approve positives) — outcome-based learning,
no classifier and no clicks.

Note this is only the verification-DEPENDENT negative signal. The
verification-INDEPENDENT one — the agent reverting an edit Hedwig just
auto-applied — is detected in hedwig-record.py (PostToolUse), so the loop
produces negative signal even with no HEDWIG_VERIFY_CMD configured.

Verification command source (first found wins):
  * env HEDWIG_VERIFY_CMD
  * <data_dir>/verify_cmd.txt  (one shell command)
If neither is set, the hook is a no-op (verification is opt-in).

ZERO-DEP: TrustDB.record_trace + policy_history only. Never materializes a
classifier. Always exits 0 — a verification hiccup must not wedge the turn.
The Stop-block path (keeping Claude working to fix failures) is deferred to
the protocol work; for now we only record outcome history.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _hedwig_common import (  # noqa: E402
    DECISIONS_LOG,
    _iter_jsonl,
    append_jsonl,
    data_dir,
    ensure_learned_interpreter,
    open_trust_db,
    policy_input_for_decision,
    policy_input_for_regret,
    repo_root_key,
    update_classifier_for_decision,
    update_classifier_for_regret,
)


def _verify_cmd() -> str | None:
    env = os.environ.get("HEDWIG_VERIFY_CMD")
    if env and env.strip():
        return env.strip()
    f = data_dir() / "verify_cmd.txt"
    if f.exists():
        try:
            txt = f.read_text(encoding="utf-8").strip()
            return txt or None
        except Exception:
            return None
    return None


def _changed_files(cwd: str) -> set[str] | None:
    """Repo-relative paths with uncommitted changes (tracked + untracked).

    The set of files plausibly responsible for a verification failure. Returns
    None when we can't determine it (not a git repo, git missing, error) — the
    caller treats None as "cannot scope" and records NOTHING rather than
    falsely blaming every auto-applied file in the session (R1 fix a).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    changed: set[str] = set()
    # -z output: each record is "XY <path>\0". A rename/copy (status R/C) adds
    # a SECOND \0-delimited field — the source path, with NO "XY " prefix. We
    # must consume that field, not treat it as another record: blindly slicing
    # [3:] off the bare source path corrupts it (drops its first 3 chars) and
    # loses the real old path. Walk the fields with an index so a rename/copy
    # record can skip its trailing source field.
    fields = result.stdout.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        if len(entry) < 4:
            i += 1
            continue
        status = entry[:2]
        path = entry[3:].strip()
        if path:
            changed.add(path)
        # R (rename) / C (copy) in either column → the next field is the source
        # path for this record; consume it so it isn't mis-parsed as a record.
        if "R" in status or "C" in status:
            i += 2
        else:
            i += 1
    return changed


def _auto_applied_files(session_id: str, cwd: str) -> list[str]:
    """Files recorded as auto_approve this session, from traces.jsonl."""
    seen: list[str] = []
    for row in _iter_jsonl("traces.jsonl"):
        if (
            row.get("session_id") == session_id
            and row.get("user_decision") == "auto_approve"
            and row.get("file_path")
            and row["file_path"] not in seen
        ):
            seen.append(row["file_path"])
    return seen


def _infer_developer_denials(session_id: str, repo_root: str) -> list[dict]:
    """Recover approve/deny signal from surfaced edits.

    The CLI sees approve/deny clicks directly. The plugin can't — Claude Code
    owns the native prompt. But we can infer denials at session end:

    - surfaced edit in decisions.jsonl → developer saw the native prompt
    - corresponding entry in traces.jsonl (user_decision=approve) → approved
    - no traces.jsonl entry for that file → the edit never executed → denied

    This recovers the bulk of the CLI's per-decision learning signal from
    outcome evidence alone. Returns a list of decision rows for files that
    were surfaced but never executed (inferred denials).
    """
    # Files that executed (positive trace exists).
    executed: set[str] = set()
    for row in _iter_jsonl("traces.jsonl"):
        if row.get("session_id") == session_id and row.get("file_path"):
            executed.add(row["file_path"])

    # Surfaced decisions with no execution = inferred denial.
    denied: list[dict] = []
    seen: set[str] = set()
    for row in _iter_jsonl(DECISIONS_LOG, reverse=True):
        if row.get("session_id") != session_id:
            continue
        fp = row.get("file_path") or ""
        if not fp or fp in seen:
            continue
        seen.add(fp)
        if row.get("verdict") == "surfaced" and fp not in executed:
            denied.append(row)
    return denied


def _record_inferred_denials(session_id: str, repo_root: str) -> None:
    """Record inferred developer denials as negative learning signals.

    Surfaced edits the developer chose not to apply are negative outcome
    history — the developer saw the change and said no. Feed each one as a
    negative classifier update (count_sample=True so it counts toward the
    learned-scorer threshold — developer denials are real decisions, not
    corrections like regrets).

    Best-effort: never raises into the hook.
    """
    denied_rows = _infer_developer_denials(session_id, repo_root)
    if not denied_rows:
        return
    try:
        db = open_trust_db()
        for row in denied_rows:
            fp = row.get("file_path") or ""
            if not fp:
                continue
            # Write a sentinel to traces.jsonl FIRST so that if Stop fires
            # again in this session (multiple turns), _infer_developer_denials
            # sees this file in the `executed` set and skips it. Without this,
            # the same denial would be re-recorded on every subsequent Stop.
            append_jsonl(
                "traces.jsonl",
                {
                    "session_id": session_id,
                    "cwd": repo_root,
                    "file_path": fp,
                    "user_decision": "deny",
                    "signal": "inferred_denial",
                },
            )
            db.record_trace(
                repo_root=repo_root,
                session_id=session_id,
                task=repo_root,
                stage="apply",
                action_type="file_update",
                file_path=fp,
                change_type=row.get("change_pattern"),
                diff_size=int(row.get("diff_size") or 0) or None,
                blast_radius=int(row.get("blast_radius") or 0) or None,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=float(row.get("score") or 0.0),
                user_decision="deny",
                user_feedback_text="inferred: surfaced edit never executed",
                is_security_sensitive=bool(row.get("is_security_sensitive")),
            )
            # Negative classifier update — counts as a real sample.
            pi = policy_input_for_decision(db, repo_root, fp, row)
            if pi is not None:
                update_classifier_for_decision(db, repo_root, pi, approved=False)
    except Exception:
        pass


def _notify_ready_hypothesis(session_id: str, cwd: str) -> None:
    """If the hypothesis bank has a candidate ready to surface, tell the
    developer via Stop additionalContext to run /hedwig-learn.

    Hooks are non-interactive — they can't pop a y/n prompt — so the plugin's
    hypothesis-confirmation surface is the /hedwig-learn slash command. This
    end-of-turn nudge is the only channel to let the developer know one is
    waiting. Best-effort: emits nothing on any failure (never blocks Stop).
    Writes the additionalContext JSON to stdout (the hook's single emit).
    """
    if not cwd:
        return
    repo_root = repo_root_key(cwd)  # canonical DB key (matches /hedwig-learn)
    try:
        from sc.hypothesis_bank import get_ready_hypothesis  # noqa: PLC0415

        db = open_trust_db()
        hypothesis = get_ready_hypothesis(
            trust_db=db, repo_root=repo_root, session_id=session_id
        )
        if hypothesis is None:
            return
        if db.session_has_confirmed_hypothesis(repo_root, session_id, driver=hypothesis.driver):
            return
    except Exception:
        return
    msg = (
        "Hedwig noticed a pattern in how you've been working and has a "
        "suggestion ready. Run /hedwig-learn to review and confirm or decline it."
    )
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": msg,
        },
    }))


def _run_verification(payload: dict) -> None:
    """Run the configured verification command and, on failure, record a
    negative-outcome trace (+ classifier gradient) for the auto-applied files in
    the failing change. No stdout — pure side effects. No-op when verification
    isn't configured. Best-effort: swallows all failures."""
    cmd = _verify_cmd()
    if not cmd:
        return  # verification is opt-in; nothing to do

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    repo_root = repo_root_key(cwd)  # DB key (cwd stays raw for git/subprocess)

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=120
        )
    except Exception:
        return  # can't run verification → record nothing

    if result.returncode == 0:
        return  # verification passed → no negative signal

    # Verification failed → record a negative-outcome trace, but ONLY for the
    # auto-applied files that are actually part of the failing change (R1 fix
    # a). Blaming every file auto-applied this session is false attribution:
    # an unrelated clean file shouldn't tighten because some other edit broke
    # the build. Scope to the working-tree diff.
    auto_applied = _auto_applied_files(session_id, cwd)
    if not auto_applied:
        return

    changed = _changed_files(cwd)
    if changed is None:
        # Can't determine the failing change (no git / git error). Record
        # nothing rather than over-attribute — conservative by design.
        return
    failed_files = [f for f in auto_applied if f in changed]
    if not failed_files:
        return

    append_jsonl(
        "regret.jsonl",
        {"session_id": session_id, "cwd": cwd, "files": failed_files, "verify_cmd": cmd},
    )

    try:
        db = open_trust_db()
        for rel in failed_files:
            db.record_trace(
                repo_root=repo_root,
                session_id=session_id,
                task=repo_root,
                stage="apply",
                action_type="file_update",
                file_path=rel,
                change_type=None,
                diff_size=None,
                blast_radius=None,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=0.0,
                user_decision="deny",  # negative outcome: post-apply verification failed
                user_feedback_text="verification failed after auto-apply",
                verification_passed=False,
            )
            # Corrective classifier gradient (S5), once per (session, file).
            # The trace tightens THIS file via per-file history; the classifier
            # update generalizes the negative signal to similar edits elsewhere.
            pi = policy_input_for_regret(db, repo_root, session_id, rel)
            update_classifier_for_regret(
                db, repo_root, pi, regret_key=f"verify_fail:{session_id}:{rel}"
            )
    except Exception:
        pass


def main() -> int:
    """Top-level guard — the Stop hook must never exit non-zero or it would
    wedge the turn. Any failure, including a non-UTF8/binary stdin pipe that
    makes sys.stdin.read() raise, is swallowed to exit 0. Verification and the
    hypothesis nudge are best-effort."""
    try:
        return _main_inner()
    except Exception:
        return 0


def _main_inner() -> int:
    # Re-exec under a deps-capable interpreter before reading stdin, so the
    # verification-failure regret feeds the real learned classifier at the
    # booth (update_classifier_for_regret) instead of degrading to heuristic.
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

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    repo_root = repo_root_key(cwd)

    # Run verification first (pure side effects, no stdout).
    _run_verification(payload)

    # Infer developer denials: surfaced edits with no PostToolUse = denied.
    # This recovers the approve/deny learning signal the CLI gets from clicks.
    _record_inferred_denials(session_id, repo_root)

    # Surface a ready hypothesis if one is waiting. The notification is the
    # hook's single stdout emit — independent of verification and denial inference.
    _notify_ready_hypothesis(session_id, cwd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
