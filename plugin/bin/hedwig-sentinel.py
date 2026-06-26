#!/usr/bin/env python3
"""Hedwig sentinel — log every hook payload, verify nothing.

Day 1 instrument: empirically verify which hook events Claude Code v2.1.159
actually fires, with what payload shape, in our local install. The research
subagent's claims include some events whose existence we want to confirm
before depending on them.

Reads the hook payload from stdin, appends a timestamped JSONL row to
~/.claude/plugins/data/hedwig/sentinel.jsonl, exits 0 with no output so
hook semantics are unaffected. Safe to leave running in production — it's
log-only and never writes to stdout.

Argv: hedwig-sentinel.py <hook_event_label>
The label is what we wrote in hooks.json (e.g. "PreToolUse"); we record it
alongside the payload's own hook_event_name so we can detect mismatches.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _data_dir() -> Path:
    """Resolve the plugin data dir.

    Claude Code sets ${CLAUDE_PLUGIN_DATA} for plugins; fall back to a sane
    default for `bin/` invocations outside the plugin runtime so the script
    is also runnable in unit tests."""
    raw = os.environ.get("CLAUDE_PLUGIN_DATA")
    if raw:
        return Path(raw)
    return Path.home() / ".claude" / "plugins" / "data" / "hedwig"


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw_payload = sys.stdin.read()

    payload: object
    try:
        payload = json.loads(raw_payload) if raw_payload.strip() else None
    except json.JSONDecodeError:
        payload = {"_raw": raw_payload, "_parse_error": True}

    record = {
        "ts": time.time(),
        "hook_label": label,
        "payload": payload,
        "env": {
            "CLAUDE_PLUGIN_ROOT": os.environ.get("CLAUDE_PLUGIN_ROOT"),
            "CLAUDE_PLUGIN_DATA": os.environ.get("CLAUDE_PLUGIN_DATA"),
            "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
        },
    }

    data_dir = _data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / "sentinel.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        # Never let a logging failure break a hook. Sentinel is best-effort.
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
