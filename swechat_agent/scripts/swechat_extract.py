"""
Extract SWE-chat sessions into Hedwig-shaped per-session JSONL files.

Reads from HuggingFace (SALT-NLP/SWE-chat) via the datasets library.
Writes per-session JSONL under data/swechat/sessions/<session_id>.jsonl
and a summary at data/swechat/summary.json.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from collections import defaultdict

# Allow importing from sc/ sibling
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

HERE = pathlib.Path(__file__).resolve().parent.parent  # swechat_agent/
SESSIONS_DIR = HERE / "data" / "swechat" / "sessions"
SUMMARY_PATH = HERE / "data" / "swechat" / "summary.json"

SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

# SWE-chat prompt_pushback → Hedwig user_decision
_PUSHBACK_TO_DECISION = {
    "non_pushback": "approve",
    "correction": "approve_with_feedback",
    "rejection": "deny",
    "failure_report": "deny",
    "pacing_complaint": "approve_with_feedback",
    "takeover": "interrupt",
    "requirement_change": "approve_with_feedback",
}

# SWE-chat user_persona (title case) → normalised snake_case
_PERSONA_NORMALISE = {
    "Expert Nitpicker": "expert_nitpicker",
    "Vague Requester": "vague_requester",
    "Mind Changer": "mind_changer",
    "Other": "other",
}

# file-extension → change_type heuristic
def _change_type_from_files(files_touched_json: str | None) -> str:
    if not files_touched_json:
        return "unknown"
    try:
        files = json.loads(files_touched_json) if isinstance(files_touched_json, str) else files_touched_json
    except (json.JSONDecodeError, TypeError):
        return "unknown"
    if not files:
        return "unknown"
    exts = {pathlib.Path(f).suffix.lower() for f in files if f}
    if exts & {".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte"}:
        return "typescript_javascript"
    if exts & {".py"}:
        return "python"
    if exts & {".go"}:
        return "go"
    if exts & {".rs"}:
        return "rust"
    if exts & {".java", ".kt", ".scala"}:
        return "jvm"
    if exts & {".md", ".rst", ".txt"}:
        return "docs"
    if exts & {".json", ".yaml", ".yml", ".toml"}:
        return "config"
    return "other"


def _swechat_mode_from_pct(agent_pct) -> str:
    if agent_pct is None:
        return "collaborative"
    try:
        pct = float(agent_pct)
    except (TypeError, ValueError):
        return "collaborative"
    if pct >= 90:
        return "vibe"
    if pct > 10:
        return "collaborative"
    return "human_only"


def _edit_distance_from_pct(agent_pct) -> float:
    if agent_pct is None:
        return 0.0
    try:
        pct = float(agent_pct)
    except (TypeError, ValueError):
        return 0.0
    # proxy: 1 - agent_pct means human rewrote this fraction
    return max(0.0, min(1.0, 1.0 - pct / 100.0))


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------

def extract(limit: int | None = None) -> None:
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: 'datasets' not installed. Run: pip install datasets", file=sys.stderr)
        sys.exit(1)

    print("Loading SWE-chat sessions table...")
    sessions_ds = load_dataset("SALT-NLP/SWE-chat", "sessions", split="train")
    print(f"  {len(sessions_ds)} sessions loaded")

    # Build session lookup: session_id → {user_persona, agent_percentage, files_touched}
    session_meta: dict[str, dict] = {}
    for row in sessions_ds:
        sid = row.get("session_id") or row.get("session_id")
        if not sid:
            continue
        session_meta[sid] = {
            "user_persona": row.get("user_persona"),
            "agent_percentage": row.get("agent_percentage"),
            "files_touched": row.get("files_touched"),
            "session_success": row.get("session_success"),
        }
    print(f"  Indexed {len(session_meta)} sessions")

    print("Loading SWE-chat conversations table (user_prompt rows only)...")
    conversations_ds = load_dataset("SALT-NLP/SWE-chat", "conversations", split="train")
    print(f"  {len(conversations_ds)} total conversation rows")

    # Filter to user_prompt rows with annotated pushback
    # Group by session_id
    session_turns: dict[str, list[dict]] = defaultdict(list)
    skipped_no_session = 0
    total_user_prompts = 0

    for row in conversations_ds:
        if row.get("turn_type") != "user_prompt":
            continue
        total_user_prompts += 1
        sid = row.get("session_id")
        if not sid or sid not in session_meta:
            skipped_no_session += 1
            continue
        session_turns[sid].append(dict(row))

        if limit and len(session_turns) >= limit:
            break

    print(f"  {total_user_prompts} user_prompt rows found")
    print(f"  {skipped_no_session} skipped (no session metadata)")
    print(f"  {len(session_turns)} sessions with ≥1 user_prompt turn")

    # Write per-session JSONL
    written_sessions = 0
    skipped_too_short = 0
    total_turns_written = 0

    # Stats for summary
    mode_dist: dict[str, int] = defaultdict(int)
    persona_dist: dict[str, int] = defaultdict(int)
    pushback_dist: dict[str, int] = defaultdict(int)

    for sid, turns in session_turns.items():
        meta = session_meta[sid]
        agent_pct = meta.get("agent_percentage")
        swechat_mode = _swechat_mode_from_pct(agent_pct)
        edit_dist = _edit_distance_from_pct(agent_pct)
        raw_persona = meta.get("user_persona")
        swechat_persona = _PERSONA_NORMALISE.get(raw_persona, "other") if raw_persona else None
        change_type = _change_type_from_files(meta.get("files_touched"))

        rows = []
        for turn in sorted(turns, key=lambda t: t.get("turn_number", 0)):
            pushback = turn.get("prompt_pushback")  # may be None
            user_decision = _PUSHBACK_TO_DECISION.get(pushback or "", "approve")
            feedback_text = ""
            if pushback and pushback not in ("non_pushback", None):
                feedback_text = turn.get("content") or ""

            row = {
                "session_id": sid,
                "task": (turn.get("content") or "")[:2000],  # cap length
                "stage": "apply",  # heuristic default
                "user_decision": user_decision,
                "edit_distance": edit_dist,
                "user_feedback_text": feedback_text[:1000],
                "change_type": change_type,
                "turn_number": turn.get("turn_number"),
                # SWE-chat ground-truth shadow fields
                "_swechat_pushback": pushback,
                "_swechat_persona": swechat_persona,
                "_swechat_mode": swechat_mode,
                "_swechat_intent": turn.get("prompt_intent"),
                "_swechat_success": meta.get("session_success"),
                "_swechat_agent_pct": agent_pct,
            }
            rows.append(row)
            if pushback:
                pushback_dist[pushback] += 1
            else:
                pushback_dist["_unannotated"] += 1

        if len(rows) < 1:
            skipped_too_short += 1
            continue

        out_path = SESSIONS_DIR / f"{sid}.jsonl"
        with open(out_path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

        written_sessions += 1
        total_turns_written += len(rows)
        mode_dist[swechat_mode] += 1
        if swechat_persona:
            persona_dist[swechat_persona] += 1
        else:
            persona_dist["_unannotated"] += 1

    print(f"\nExtraction complete:")
    print(f"  {written_sessions} sessions written")
    print(f"  {total_turns_written} turns total")
    print(f"  {skipped_too_short} sessions skipped (no turns)")

    summary = {
        "n_sessions": written_sessions,
        "n_turns": total_turns_written,
        "skipped_no_session_meta": skipped_no_session,
        "coding_mode_distribution": dict(mode_dist),
        "user_persona_distribution": dict(persona_dist),
        "pushback_type_distribution": dict(pushback_dist),
        "edit_distance_proxy": "session-level: 1.0 - (agent_percentage / 100)",
        "stage_proxy": "hardcoded 'apply' — SWE-chat has no stage signal",
    }
    with open(SUMMARY_PATH, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  Summary written to {SUMMARY_PATH}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after this many sessions (for dev/test)")
    args = p.parse_args()
    extract(limit=args.limit)
