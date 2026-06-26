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
    _iter_jsonl,
    append_jsonl,
    data_dir,
    ensure_learned_interpreter,
    open_trust_db,
    policy_input_for_regret,
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
    # -z output: each entry is "XY <path>\0" (rename adds a second \0 path).
    for entry in result.stdout.split("\0"):
        if len(entry) < 4:
            continue
        path = entry[3:].strip()
        if path:
            changed.add(path)
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


def main() -> int:
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

    cmd = _verify_cmd()
    if not cmd:
        return 0  # verification is opt-in; nothing to do

    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()

    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=120
        )
    except Exception:
        return 0  # can't run verification → record nothing

    if result.returncode == 0:
        return 0  # verification passed → no negative signal

    # Verification failed → record a negative-outcome trace, but ONLY for the
    # auto-applied files that are actually part of the failing change (R1 fix
    # a). Blaming every file auto-applied this session is false attribution:
    # an unrelated clean file shouldn't tighten because some other edit broke
    # the build. Scope to the working-tree diff.
    auto_applied = _auto_applied_files(session_id, cwd)
    if not auto_applied:
        return 0

    changed = _changed_files(cwd)
    if changed is None:
        # Can't determine the failing change (no git / git error). Record
        # nothing rather than over-attribute — conservative by design.
        return 0
    failed_files = [f for f in auto_applied if f in changed]
    if not failed_files:
        return 0

    append_jsonl(
        "regret.jsonl",
        {"session_id": session_id, "cwd": cwd, "files": failed_files, "verify_cmd": cmd},
    )

    try:
        db = open_trust_db()
        for rel in failed_files:
            db.record_trace(
                repo_root=cwd,
                session_id=session_id,
                task=cwd,
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
            pi = policy_input_for_regret(db, cwd, session_id, rel)
            update_classifier_for_regret(
                db, cwd, pi, regret_key=f"verify_fail:{session_id}:{rel}"
            )
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
