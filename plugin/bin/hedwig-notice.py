#!/usr/bin/env python3
"""Hedwig hypothesis-noticer intake — agent-proposed standing rules for the plugin.

The CLI has a Bedrock "noticer" that reads a session's decision traces and
proposes candidate preferences / guidelines / repo facts. The plugin is local
(no Bedrock), so the reasoning is done by Claude Code itself, guided by the
`repo-hypotheses` skill: the agent reads its own recent traces and proposes
observations, then pipes them here. This bin does NOT reason — it validates and
ingests, reusing the exact same grounding gate and bank-insertion logic the CLI
noticer uses (sc.hypothesis_bank.ingest_llm_hypotheses).

The grounding rule is the anti-hallucination gate: every proposed candidate must
cite ≥1 REAL trace ID from this session, or it is dropped. Nothing here changes
governance behavior — candidates land as PENDING in the hypothesis bank and only
affect decisions after the developer confirms one via /hedwig-learn.

Input (JSON on stdin), produced by the skill:
    {
      "cwd": "/abs/project/root",
      "session_id": "...",
      "candidates": [
        {"type": "preference"|"behavioral_guideline"|"logic_note",
         "text": "the rule / question / fact",
         "driver": "snake_case_unique_name",
         "rationale": "one sentence grounded in the cited traces",
         "evidence_trace_ids": [12, 17],
         "high_stakes": false}
      ]
    }

Always exits 0; a noticer failure must never break the session. Local, no cloud.
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


def main(argv: list[str]) -> int:
    """Top-level guard — a skill-invoked intake must never exit non-zero."""
    try:
        if argv and argv[0] == "traces":
            return _cmd_traces(argv[1:])
        return _main_inner()
    except Exception:
        return 0


def _cmd_traces(argv: list[str]) -> int:
    """Print this session's decision traces as a digest, one line per trace with
    its [id] prefix. The agent reads this (step 1 of the repo-hypotheses skill),
    reasons over the patterns, then pipes proposed candidates back to this bin's
    stdin mode (step 2), citing the [id]s it sees here."""
    # cwd + session passed as args so the agent can pass ${CLAUDE_PROJECT_DIR}
    # / ${CLAUDE_SESSION_ID}; fall back to env/getcwd.
    cwd = argv[0] if argv else ""
    session_id = argv[1] if len(argv) > 1 else ""
    repo_root = repo_root_key(cwd)
    try:
        from sc.hypothesis_bank import _format_trace_digest  # noqa: PLC0415

        db = open_trust_db()
        traces = db.session_traces(repo_root, session_id)
        if not traces:
            sys.stdout.write("(no decision traces yet this session)\n")
            return 0
        sys.stdout.write(_format_trace_digest([dict(t) for t in traces]) + "\n")
    except Exception:
        sys.stdout.write("(could not read traces)\n")
    return 0


def _main_inner() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        sys.stdout.write("Hedwig noticer: no input.\n")
        return 0
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.stdout.write("Hedwig noticer: could not parse input.\n")
        return 0
    if not isinstance(payload, dict):
        return 0

    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        sys.stdout.write("Hedwig noticer: no candidates proposed.\n")
        return 0

    repo_root = repo_root_key(payload.get("cwd") or "")
    session_id = str(payload.get("session_id") or "")

    try:
        from sc.hypothesis_bank import ingest_llm_hypotheses  # noqa: PLC0415

        db = open_trust_db()
        # Validate citations against THIS session's real traces — the same
        # grounding gate the CLI noticer uses. A candidate citing no real trace
        # is dropped.
        traces = db.session_traces(repo_root, session_id)
        if not traces:
            sys.stdout.write(
                "Hedwig noticer: no traces yet this session, nothing to ground "
                "hypotheses against.\n"
            )
            return 0
        valid_trace_ids = {int(t["id"]) for t in traces}
        new_ids = ingest_llm_hypotheses(
            trust_db=db,
            repo_root=repo_root,
            session_id=session_id,
            candidates_data=candidates,
            traces=[dict(t) for t in traces],
            valid_trace_ids=valid_trace_ids,
        )
    except Exception:
        sys.stdout.write("Hedwig noticer: storage unavailable; nothing recorded.\n")
        return 0

    n = len(new_ids)
    sys.stdout.write(
        f"Hedwig noticer: recorded {n} grounded candidate{'' if n == 1 else 's'}. "
        "They're pending in the hypothesis bank. Review and confirm with "
        "/hedwig-learn before any affects a decision. Uncited proposals were "
        "dropped.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
