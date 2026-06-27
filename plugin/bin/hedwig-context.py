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
import sys
from pathlib import Path

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
        return 0
    if not summary:
        return 0
    lead = (
        "Hedwig — what we've learned about this repo so far "
        f"(from prior sessions): {summary}"
    )
    return _emit("SessionStart", lead)


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
    return _emit("UserPromptSubmit", "Hedwig memory — " + "\n".join(lines))


def main(argv: list[str]) -> int:
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
