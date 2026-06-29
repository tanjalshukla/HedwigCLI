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
    DENIED_VERDICT,
    data_dir,
    learned_scorer_reachable,
    open_trust_db,
    owl_str,
    repo_root_key,
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
                if not isinstance(row, dict):
                    continue  # skip non-object lines (a bare int/list/str/null)
                if session_id is None or row.get("session_id") == session_id:
                    rows.append(row)
    except Exception:
        return rows
    return rows


def _summarize(rows: list[dict]) -> dict:
    suppressed = sum(1 for r in rows if r.get("verdict") == "suppressed")
    surfaced = sum(1 for r in rows if r.get("verdict") == "surfaced")
    denied = sum(1 for r in rows if r.get("verdict") == DENIED_VERDICT)
    total = suppressed + surfaced + denied
    rate = (suppressed / total) if total else 0.0
    return {
        "total": total,
        "suppressed": suppressed,
        "surfaced": surfaced,
        "denied": denied,
        "suppression_rate": round(rate, 3),
    }


def _bar(rate: float, width: int = 20) -> str:
    filled = int(round(rate * width))
    return "█" * filled + "░" * (width - filled)


def _surfaced_reasons(rows: list[dict]) -> list[str]:
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    for r in reversed(rows):  # most recent first
        if r.get("verdict") != "surfaced":
            continue
        reason = (r.get("reason") or "").strip()
        fp = r.get("file_path") or ""
        if not reason:
            continue
        key = (fp, reason)
        if key in seen:
            continue
        seen.add(key)
        out.append(reason)
        if len(out) >= 4:
            break
    return out


def _regret_count() -> int:
    """Count regret events from regret.jsonl for this repo."""
    repo = repo_root_key(None)
    count = 0
    from _hedwig_common import _iter_jsonl  # noqa: PLC0415
    for row in _iter_jsonl("regret.jsonl"):
        row_cwd = row.get("cwd")
        if row_cwd and repo_root_key(row_cwd) != repo:
            continue
        count += 1
    return count


def _classifier_samples() -> int | None:
    """Return the classifier's sample_count for this repo, or None if unavailable."""
    if not learned_scorer_reachable():
        return None
    try:
        db = open_trust_db()
        classifier = db.load_policy_model(repo_root_key(None))
        return classifier.sample_count if classifier is not None else None
    except Exception:
        return None


def _render(summary: dict, rows: list[dict], scope: str) -> str:
    total = summary["total"]
    if total == 0:
        lines = [
            owl_str(),
            "",
            "Hedwig — trust runtime",
            "",
            f"No edits governed yet ({scope}). Make a few edits and run /hedwig-status.",
        ]
        return "\n".join(lines)

    pct = int(round(summary["suppression_rate"] * 100))
    denied = summary.get("denied", 0)
    lines = [
        owl_str(),
        "",
        "Hedwig — trust runtime",
        "",
        f"  Auto-applied   {summary['suppressed']:>3}   {_bar(summary['suppression_rate'])}  {pct}%",
        f"  Surfaced       {summary['surfaced']:>3}   for your review",
    ]
    if denied:
        lines.append(f"  Blocked        {denied:>3}   agent asked to revise")
    lines.append("")
    lines.append(
        f"  {summary['suppressed']} of {total} edit{'s' if total != 1 else ''} "
        f"auto-applied {scope}."
    )

    reasons = _surfaced_reasons(rows)
    if reasons:
        lines.append("")
        lines.append("  Why it surfaced these:")
        for r in reasons:
            lines.append(f"    · {r}")

    regrets = _regret_count()
    if regrets:
        lines.append("")
        lines.append(
            f"  {regrets} self-correction{'s' if regrets != 1 else ''}: "
            "auto-applied edits later reverted or failing verification."
        )
        lines.append("  Each one tightened the next similar edit. Run /hedwig-retrospective to see them.")

    samples = _classifier_samples()
    if samples is not None:
        # samples is non-None only when learned_scorer_reachable() is True, so
        # numpy/sklearn import cleanly here. Importing at module top (or before
        # this guard) would crash the whole command under the default plugin
        # runtime, where the hooks run under a bare python without the deps.
        from sc.ml_policy import MIN_SAMPLES_FOR_LEARNED  # noqa: PLC0415
        if samples >= MIN_SAMPLES_FOR_LEARNED:
            lines.append("")
            lines.append(f"  Learned scorer active ({samples} decisions recorded).")
        else:
            remaining = MIN_SAMPLES_FOR_LEARNED - samples
            lines.append("")
            lines.append(
                f"  Learned scorer: {samples}/{MIN_SAMPLES_FOR_LEARNED} decisions — "
                f"{remaining} more until it takes over from the heuristic."
            )
    elif not learned_scorer_reachable():
        lines.append("")
        lines.append("  ⚠ Heuristic-only — run /hedwig-setup once to enable learning.")

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
