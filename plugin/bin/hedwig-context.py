#!/usr/bin/env python3
"""Hedwig memory-layer injector — feeds repo memory into the model's context.

This is the plugin's delivery channel for the "what we've learned about this
repo" memory layer (the CLI builds it into the system prompt; the plugin can't
touch that prompt, but Claude Code's SessionStart / UserPromptSubmit hooks let a
plugin inject `additionalContext` that the MODEL reads). Two modes, selected by
argv:

  SessionStart  -> a one-paragraph orientation lead synthesized from confirmed
                   preferences + repo facts (logic notes) + recent feedback
                   (sc.repo_memory.synthesize_repo_summary — the SAME function
                   the CLI uses, so the two front-ends never drift).

  UserPromptSubmit -> task-relevant behavioral guidelines + logic notes,
                   retrieved by overlap with the submitted prompt (the vendored
                   Retrieval seam: embedding when fastembed is present, keyword
                   otherwise).

Emits a single JSON object on stdout:
    {"hookSpecificOutput": {"hookEventName": "<event>",
                            "additionalContext": "<text>"}}
When there's nothing to say, emits nothing (exit 0) so no empty reminder is
injected. Best-effort throughout: any failure exits 0 with no output — a memory
hiccup must never block the session or the prompt. Output is capped well under
Claude Code's 10K additionalContext limit. No Bedrock, no credentials.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Force keyword retrieval on this hot path. This hook runs on EVERY
# UserPromptSubmit / SessionStart as a fresh subprocess, so the fastembed
# embedding cache never warms — materializing the model would add ~5s to every
# prompt. Keyword ranking is instant and good enough at plugin scale. Set before
# importing the retrieval seam so select_ranker honors it. (The long-lived CLI
# keeps embeddings; only this per-invocation subprocess opts out.)
os.environ.setdefault("HEDWIG_DISABLE_EMBEDDINGS", "1")

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _hedwig_common import open_trust_db, repo_root_key  # noqa: E402

# Keep injected context well under Claude Code's 10K additionalContext cap.
_MAX_CONTEXT_CHARS = 4000


def _emit(event: str, text: str) -> int:
    """Emit additionalContext for `event`, or nothing if text is empty."""
    text = (text or "").strip()
    if not text:
        return 0  # nothing to inject → no empty system reminder
    if len(text) > _MAX_CONTEXT_CHARS:
        text = text[:_MAX_CONTEXT_CHARS].rstrip() + " …"
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        },
    }))
    return 0


def _session_start(payload: dict) -> int:
    """One-paragraph "what we've learned about this repo" lead at session open."""
    cwd = payload.get("cwd") or ""
    if not cwd:
        return 0
    repo_root = repo_root_key(cwd)  # canonical DB key (matches the slash commands)
    try:
        from sc.repo_memory import synthesize_repo_summary  # noqa: PLC0415

        db = open_trust_db()
        logic_notes = [n.note for n in db.recent_logic_notes(repo_root, limit=3)]
        feedback = db.recent_feedback_snippets(repo_root, limit=2)
        summary = synthesize_repo_summary(
            trust_db=db,
            repo_root=repo_root,
            logic_note_lines=logic_notes,
            feedback_snippets=list(feedback),
        )
    except Exception:
        db = None
        summary = ""

    parts: list[str] = []
    if summary:
        parts.append(
            "Hedwig · what we've learned about this repo so far "
            f"(from prior sessions): {summary}"
        )
    # First session in this repo (no agent scan yet) → invite a one-time scan so
    # Hedwig can flag security-sensitive files keyword matching would miss. The
    # repo-scan skill carries the how/when; this is just the trigger.
    if _should_invite_scan(db, repo_root):
        parts.append(
            "Hedwig · this repo hasn't been scanned yet. Early in your work here, "
            "use the repo-scan skill once to flag security-sensitive files (and "
            "note durable repo facts) so Hedwig governs edits to them correctly."
        )
    if _should_invite_setup():
        parts.append(
            "Hedwig · the online classifier isn't active yet. Run /hedwig-setup "
            "once to enable it — it builds a small local venv and the learned "
            "scorer starts improving from your decisions automatically."
        )
    if not parts:
        return 0
    return _emit("SessionStart", "\n\n".join(parts))


def _should_invite_scan(db, repo_root: str) -> bool:
    """True if no agent scan has run for this repo yet."""
    if db is None:
        return False
    try:
        return not db.security_paths(repo_root)
    except Exception:
        return False


def _should_invite_setup() -> bool:
    """True if the learned-scorer venv doesn't exist yet. Best-effort: any
    failure returns False so a missing venv check never blocks the session."""
    try:
        from _hedwig_common import learned_scorer_reachable  # noqa: PLC0415
        return not learned_scorer_reachable()
    except Exception:
        return False


def _user_prompt_submit(payload: dict) -> int:
    """Task-relevant guidelines + repo facts retrieved by overlap with the
    submitted prompt, injected alongside it before the model responds."""
    cwd = payload.get("cwd") or ""
    query = (payload.get("prompt") or "").strip()
    if not cwd or not query:
        return 0
    repo_root = repo_root_key(cwd)  # canonical DB key (matches the slash commands)
    try:
        db = open_trust_db()
        guidelines = db.relevant_behavioral_guidelines(
            repo_root, query_text=query, limit=4
        )
        notes = db.relevant_logic_notes(repo_root, query_text=query, limit=3)
    except Exception:
        return 0

    lines: list[str] = []
    g_text = [getattr(g, "guideline", str(g)).strip() for g in guidelines]
    g_text = [g for g in g_text if g]
    if g_text:
        lines.append("Relevant guidelines for this repo:")
        lines.extend(f"- {g}" for g in g_text[:4])
    n_text = [getattr(n, "note", str(n)).strip() for n in notes]
    n_text = [n for n in n_text if n]
    if n_text:
        lines.append("Relevant repo facts:")
        lines.extend(f"- {n}" for n in n_text[:3])
    if not lines:
        return 0
    return _emit("UserPromptSubmit", "Hedwig memory · " + "\n".join(lines))


def main(argv: list[str]) -> int:
    """Top-level guard — SessionStart / UserPromptSubmit hooks must never exit
    non-zero or block the prompt. Any failure, including a non-UTF8/binary stdin
    pipe that makes sys.stdin.read() raise, falls through to exit 0 with no
    output. Repo-memory injection is best-effort."""
    try:
        return _main_inner(argv)
    except Exception:
        return 0


def _main_inner(argv: list[str]) -> int:
    event = argv[0] if argv else "SessionStart"
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        if event == "UserPromptSubmit":
            return _user_prompt_submit(payload)
        return _session_start(payload)
    except Exception:
        return 0  # never block the session/prompt on a memory hiccup


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
