#!/usr/bin/env python3
"""Hedwig status — the one-number headline.

Reads the decision-event log decide.py writes and prints a compact summary:
how many prompts Hedwig suppressed (auto-applied, low-risk) vs. surfaced for
review this session. This is the booth/LinkedIn headline:

    "Hedwig suppressed 12 of 15 edit prompts this session (80%),
     surfacing the 3 it judged worth your review."

Usage:
    hedwig-status.py [--session SESSION_ID] [--json]

With --session, tallies only that session's decisions; without, tallies all
recorded decisions (all-time for this install). --json emits machine-readable
output for the slash command or tests.
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
    data_dir,
    learned_scorer_reachable,
)


def _load_decisions(session_id: str | None) -> list[dict]:
    path = data_dir() / DECISIONS_LOG
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_id is None or row.get("session_id") == session_id:
                    rows.append(row)
    except Exception:
        return rows
    return rows


def _summarize(rows: list[dict]) -> dict:
    suppressed = sum(1 for r in rows if r.get("verdict") == "suppressed")
    surfaced = sum(1 for r in rows if r.get("verdict") == "surfaced")
    total = suppressed + surfaced
    rate = (suppressed / total) if total else 0.0
    return {
        "total": total,
        "suppressed": suppressed,
        "surfaced": surfaced,
        "suppression_rate": round(rate, 3),
    }


def _bar(rate: float, width: int = 20) -> str:
    filled = int(round(rate * width))
    return "█" * filled + "░" * (width - filled)


def _surfaced_reasons(rows: list[dict]) -> list[str]:
    """Most recent surfaced-edit reasons — the 'why it stopped you' lines.
    The regret money-shot ('you reverted a similar edit') lands here."""
    out: list[str] = []
    seen: set[str] = set()
    for r in reversed(rows):  # most recent first
        if r.get("verdict") != "surfaced":
            continue
        reason = (r.get("reason") or "").strip()
        fp = r.get("file_path") or ""
        if not reason or fp in seen:
            continue
        seen.add(fp)
        out.append(reason)
        if len(out) >= 4:
            break
    return out


def _render(summary: dict, rows: list[dict], scope: str) -> str:
    total = summary["total"]
    if total == 0:
        return (
            "Hedwig hasn't governed any edits yet "
            f"({scope}). Make a few edits and run /hedwig-status again."
        )

    pct = int(round(summary["suppression_rate"] * 100))
    lines = [
        "Hedwig — trust runtime",
        "",
        f"  Auto-applied   {summary['suppressed']:>3}   {_bar(summary['suppression_rate'])}  {pct}%",
        f"  Surfaced       {summary['surfaced']:>3}   {' ' * 20}  for your review",
        "",
        f"  {summary['suppressed']} of {total} edit{'s' if total != 1 else ''} applied without a prompt {scope}; "
        f"the {summary['surfaced']} riskier one{'s' if summary['surfaced'] != 1 else ''} surfaced for review.",
    ]

    reasons = _surfaced_reasons(rows)
    if reasons:
        lines.append("")
        lines.append("  Why it surfaced these:")
        for r in reasons:
            lines.append(f"    • {r}")

    # Nudge: if the learned classifier can't run here, every decision is
    # heuristic-only. One command turns the learned scorer on for good.
    if not learned_scorer_reachable():
        lines.append("")
        lines.append("  ⚠ Running heuristic-only — the learned classifier isn't active.")
        lines.append("    Turn it on (one time): python3 plugin/bin/hedwig-setup.py")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    session_id: str | None = None
    as_json = False
    i = 0
    while i < len(argv):
        if argv[i] == "--session" and i + 1 < len(argv):
            session_id = argv[i + 1]
            i += 2
        elif argv[i] == "--json":
            as_json = True
            i += 1
        else:
            i += 1

    rows = _load_decisions(session_id)
    summary = _summarize(rows)
    scope = "this session" if session_id else "all-time"

    if as_json:
        sys.stdout.write(json.dumps({
            **summary,
            "scope": scope,
            "surfaced_reasons": _surfaced_reasons(rows),
            "learned_scorer_active": learned_scorer_reachable(),
        }))
    else:
        sys.stdout.write(_render(summary, rows, scope) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
