from __future__ import annotations

"""Post-session calibration retrospective.

Triggered by /retrospective in the REPL. Shows a single, brief panel
summarizing where Hedwig was too loose and too tight this session, then
offers one adjustment. Skippable in one keystroke.

Design principle: Hedwig offers, never demands. The panel is short,
personal, and exits immediately if the developer isn't interested.

"Too loose" = auto-approved something you later pushed back on (regret).
"Too tight" = you approved a check-in in under 3 seconds (rubber stamp).
"""

import json
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from ..regret import detect_regret_events
from .theme import PALETTE, moment, panel_title

_CONSOLE = Console()

if TYPE_CHECKING:
    from ..trust_db import TrustDB

_RUBBER_STAMP_THRESHOLD_SECONDS = 3.0


def _too_tight_files(session_rows: list[dict]) -> list[str]:
    """Files where Hedwig checked in but the developer rubber-stamped."""
    seen: set[str] = set()
    files: list[str] = []
    for row in session_rows:
        fp = row.get("file_path") or ""
        if fp in seen or fp == "__session__":
            continue
        decision = (row.get("user_decision") or "").lower()
        duration = row.get("review_duration_seconds")
        is_checkin = decision in ("approve", "approve_and_remember")
        is_fast = duration is not None and float(duration) < _RUBBER_STAMP_THRESHOLD_SECONDS
        if is_checkin and is_fast:
            files.append(fp)
            seen.add(fp)

    return files


def run_retrospective(
    *,
    trust_db: "TrustDB",
    repo_root_str: str,
    session_id: str,
) -> None:
    """Render the calibration retrospective panel and handle one optional adjustment."""
    console = _CONSOLE

    rows = [dict(r) for r in trust_db.session_traces(repo_root_str, session_id)]
    if not rows:
        console.print(f"[{PALETTE['meta']}]Nothing to review — no actions this session.[/{PALETTE['meta']}]")
        return

    regret_events = detect_regret_events(rows)
    too_loose = list(dict.fromkeys(e.file_path for e in regret_events))
    too_tight = _too_tight_files(rows)

    auto_approves = sum(1 for r in rows if (r.get("user_decision") or "").startswith("auto_approve"))
    checkins = sum(1 for r in rows if r.get("user_decision") in ("approve", "approve_and_remember", "deny"))

    # Don't show the panel if there's nothing to calibrate.
    if not too_loose and not too_tight:
        body = Text()
        body.append(
            f"Clean session — {auto_approves} auto-approved, {checkins} check-ins. Nothing to adjust.\n",
            style=PALETTE["approve"],
        )
        console.print(
            Panel(body, title=panel_title("info", "session wrap-up"),
                  border_style=moment("info").border, padding=(1, 2))
        )
        return

    body = Text()
    body.append(f"{auto_approves} auto-approved · {checkins} check-ins\n\n", style=PALETTE["meta"])

    if too_loose:
        body.append("Possibly too loose:\n", style=PALETTE["attention"])
        for fp in too_loose[:3]:
            body.append(f"  · {fp}  ", style="white")
            body.append("auto-approved, then you pushed back\n", style=PALETTE["meta"])
        if len(too_loose) > 3:
            body.append(f"  … and {len(too_loose) - 3} more\n", style=PALETTE["meta"])
        body.append("\n")

    if too_tight:
        body.append("Possibly too cautious:\n", style=PALETTE["info"])
        for fp in too_tight[:3]:
            body.append(f"  · {fp}  ", style="white")
            body.append("you approved quickly — Hedwig may not need to ask\n", style=PALETTE["meta"])
        if len(too_tight) > 3:
            body.append(f"  … and {len(too_tight) - 3} more\n", style=PALETTE["meta"])

    console.print(
        Panel(body, title=panel_title("info", "session wrap-up"),
              border_style=moment("info").border, padding=(1, 2))
    )

    # One optional adjustment — keep it light.
    if too_loose:
        try:
            pick = Prompt.ask(
                f"[{PALETTE['meta']}]Want me to check in more carefully on changes like these next time?[/{PALETTE['meta']}] (y/n)",
                choices=["y", "n"],
                default="n",
            )
        except (KeyboardInterrupt, EOFError):
            return

        if pick == "y":
            # Seed a scope_constraint-style preference directly into confirmed_preferences
            # for the affected files — bypasses the hypothesis bank since developer
            # is explicitly asking for tighter oversight.
            from ..preferences import (
                Condition, Lifecycle, Preference, PreferenceAction, Scope, Trigger
            )
            from ..preferences import preference_to_dict

            pref = Preference(
                trigger=Trigger(stages=("apply",), min_blast_radius=1),
                condition=Condition(min_prior_pushback_count=1),
                action=PreferenceAction.FULL_CHECKIN,
                scope=Scope(level="repo"),
                lifecycle=Lifecycle(provenance="user_explicit", confidence=1.0),
            )
            payload = {
                "accepted": True,
                "driver": "retrospective_tighten",
                "preference": preference_to_dict(pref),
            }
            trust_db.save_confirmed_preference(
                repo_root=repo_root_str,
                session_id=session_id,
                preference_json=json.dumps(payload),
                driver="retrospective_tighten",
            )
            console.print(
                f"[{PALETTE['learn']}]✦ Got it — I'll be more careful next time.[/{PALETTE['learn']}]"
            )
    elif too_tight and not too_loose:
        try:
            pick = Prompt.ask(
                f"[{PALETTE['meta']}]Want me to pause less on straightforward changes?[/{PALETTE['meta']}] (y/n)",
                choices=["y", "n"],
                default="n",
            )
        except (KeyboardInterrupt, EOFError):
            return

        if pick == "y":
            current = trust_db.autonomy_preferences(repo_root_str)
            from ..autonomy import merge_preferences, AutonomyPreferences
            updated, _ = merge_preferences(
                current,
                AutonomyPreferences(prefer_fewer_checkins=True),
            )
            trust_db._save_autonomy_preferences(repo_root_str, updated)
            console.print(
                f"[{PALETTE['learn']}]✦ Got it — I'll give you more room on low-risk changes.[/{PALETTE['learn']}]"
            )
