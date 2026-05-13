from __future__ import annotations

"""`hw learning` — what does Hedwig know about this repo, across all sessions?

A repo-scoped companion to `hw status`. Natural language, no tables.
Researcher depth lives in `hw observe export --html`.
"""

import json
from collections import Counter

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..commands.shared import open_trust_db, require_repo_root
from ..preference_inference import summarize_session
from ..run.theme import PALETTE, panel_title


def learning() -> None:
    """What Hedwig has picked up about this repo and the people working in it."""
    console = Console()
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)

    # Pull aggregates we need to tell the story.
    with trust_db._connect() as conn:
        total_sessions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS n FROM decision_traces WHERE repo_root = ?",
            (repo_root_str,),
        ).fetchone()["n"]
        total_turns = conn.execute(
            "SELECT COUNT(*) AS n FROM decision_traces WHERE repo_root = ?",
            (repo_root_str,),
        ).fetchone()["n"]
        confirmed_rows = conn.execute(
            """
            SELECT preference_json, driver, created_at
            FROM confirmed_preferences
            WHERE repo_root = ?
            ORDER BY created_at DESC
            """,
            (repo_root_str,),
        ).fetchall()
        constraints_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM hard_constraints WHERE repo_root = ?",
            (repo_root_str,),
        ).fetchone()
        guidelines_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM behavioral_guidelines WHERE repo_root = ?",
            (repo_root_str,),
        ).fetchone()

    classifier = trust_db.load_policy_model(repo_root_str)
    sample_count = classifier.sample_count if classifier is not None else 0

    body = Text()

    # Top-line sentence about engagement.
    if total_sessions == 0:
        body.append(
            "Nothing yet — we haven't worked together in this repo.\n\n",
            style="white",
        )
        body.append("Run ", style=PALETTE["meta"])
        body.append("hw '<task>'", style=PALETTE["info_bold"])
        body.append(" to start.", style=PALETTE["meta"])
        console.print(
            Panel(
                body,
                title=panel_title("info", "what I've learned"),
                border_style=PALETTE["info"],
                padding=(1, 2),
            )
        )
        return

    # History headline.
    body.append(
        f"We've worked together across {total_sessions} session"
        f"{'s' if total_sessions != 1 else ''} "
        f"in this repo, {total_turns} turns total.\n\n",
        style="white",
    )

    # Rules you set — hard constraints + behavioral guidelines.
    hard_n = constraints_rows["n"] if constraints_rows else 0
    soft_n = guidelines_rows["n"] if guidelines_rows else 0
    if hard_n or soft_n:
        body.append("Rules you've given me:\n", style=PALETTE["info_bold"])
        if hard_n:
            body.append(
                f"  · {hard_n} hard constraint"
                f"{'s' if hard_n != 1 else ''} "
                f"(non-negotiable)\n",
                style="white",
            )
        if soft_n:
            body.append(
                f"  · {soft_n} behavioral guideline"
                f"{'s' if soft_n != 1 else ''} "
                f"(guidance I carry into each session)\n",
                style="white",
            )
        body.append("\n")

    # Preferences you've confirmed via hypothesis.
    accepted_drivers: Counter[str] = Counter()
    for r in confirmed_rows:
        try:
            payload = json.loads(r["preference_json"])
        except Exception:
            continue
        if payload.get("accepted"):
            accepted_drivers[payload.get("driver", "unknown")] += 1

    driver_phrases = {
        "scope_constraint": "to check in before multi-file changes",
        "positive_redirect": "to soft-check-in on small follow-ups",
        "failure_reactive": "to check in on non-trivial changes when things are unstable",
        "deliberate_reviewer": "to use soft check-ins on small diffs, saving full prompts for bigger changes",
        "rapid_approver": "to always check in on larger changes so you stay in the loop on the big stuff",
    }
    if accepted_drivers:
        body.append(
            "Preferences you've confirmed when I noticed patterns:\n",
            style=PALETTE["learn_bold"],
        )
        for driver, count in accepted_drivers.most_common():
            phrase = driver_phrases.get(driver, driver)
            body.append(
                f"  · {phrase}  ", style="white",
            )
            body.append(
                f"(×{count})\n",
                style=PALETTE["meta"],
            )
        body.append("\n")
    else:
        # We've seen sessions but never successfully surfaced a pattern.
        if total_turns > 5:
            body.append(
                "I haven't surfaced any pattern hypotheses yet. I wait until "
                "I see a behavior a few times before asking to avoid being "
                "annoying.\n\n",
                style=PALETTE["meta_italic"],
            )

    # Learned scorer state in plain English.
    body.append("The scorer:\n", style=PALETTE["info_bold"])
    if sample_count >= 10:
        body.append(
            f"  · active, trained on {sample_count} of your real decisions\n",
            style=PALETTE["approve"],
        )
        if classifier is not None:
            # Find the largest absolute coefficient shift and describe it.
            import numpy as _np
            from ..ml_policy import FEATURE_NAMES as _FN

            deltas = [
                (name, float(classifier.clf.coef_[0][i] - classifier.prior_coef[i]))
                for i, name in enumerate(_FN)
            ]
            deltas.sort(key=lambda x: -abs(x[1]))
            if deltas:
                top_name, top_delta = deltas[0]
                direction = "pushed toward auto-approving" if top_delta > 0 else "pushed toward checking in"
                body.append(
                    f"  · biggest shift: {top_name.replace('_', ' ')} "
                    f"({top_delta:+.2f}) — {direction}\n",
                    style="white",
                )
    else:
        body.append(
            f"  · cold-start mode · {sample_count}/10 decisions to activate the learned model\n",
            style=PALETTE["meta"],
        )

    console.print(
        Panel(
            body,
            title=panel_title("info", "what I've learned"),
            border_style=PALETTE["info"],
            padding=(1, 2),
        )
    )
    console.print(
        f"[{PALETTE['meta']}]For researcher-level depth:[/{PALETTE['meta']}] "
        f"hw observe export --html"
    )
