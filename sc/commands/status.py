from __future__ import annotations

"""`hw status` — a developer-facing "what does Hedwig think right now" view.

No jargon. Plain sentences. Researcher-level data lives behind
`hw observe export` (HTML) or `hw observe <cmd> --verbose`.
"""

import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..commands.shared import open_trust_db, require_repo_root
from ..preference_inference import summarize_session
from ..preferences import preference_from_dict
from ..run.theme import PALETTE, panel_title
from ..status import (
    LearnedPreference,
    build_session_status,
    template_preference_line,
    template_session_sentence,
    template_proactive_pause_sentence,
)


def _humanize_preference(payload: dict, *, scope: str) -> LearnedPreference | None:
    """Turn a persisted confirmed-preference payload into a human-readable
    LearnedPreference. Returns None for non-accepted payloads."""
    if not payload.get("accepted"):
        return None
    pref_dict = payload.get("preference")
    driver = payload.get("driver", "")

    # Humanize by driver — these are the only drivers we emit today.
    driver_map = {
        "scope_constraint": (
            "I'll check in before multi-file changes",
            "You narrowed scope on me several times — I'm treating multi-file work as worth a pause.",
        ),
        "positive_redirect": (
            "I'll soft-check-in on small follow-ups",
            "You've been accepting quick small changes — I'll surface them without blocking.",
        ),
        "failure_reactive": (
            "I'll check in on non-trivial changes while things are unstable",
            "We've hit failures this session — I'm tightening oversight on larger edits until it stabilizes.",
        ),
        "deliberate_reviewer": (
            "I'll use soft check-ins on small diffs, full prompts for bigger ones",
            "You've been reviewing carefully — I'll save the full pause for changes that need your attention.",
        ),
        "rapid_approver": (
            "I'll always check in on larger changes",
            "You've been approving quickly — I'll make sure you stay in the loop on the bigger stuff.",
        ),
    }
    if driver in driver_map:
        headline, basis = driver_map[driver]
        return LearnedPreference(headline=headline, basis=basis, scope=scope)
    # Fallback — if we can deserialize, use the driver name.
    if pref_dict:
        try:
            preference_from_dict(pref_dict)
        except Exception:
            return None
        return LearnedPreference(
            headline="Adjusted check-in behavior",
            basis=f"Confirmed via hypothesis: {driver}.",
            scope=scope,
        )
    return None


def _most_recent_proactive_reason(session_rows: list[dict]) -> tuple[int, str | None]:
    """Scan session traces for Hedwig-initiated proactive pauses. Returns
    (count, most_recent_reason)."""
    count = 0
    most_recent: str | None = None
    for row in session_rows:
        if row.get("check_in_initiator") == "policy":
            count += 1
            reasons_json = row.get("policy_reasons_json")
            if reasons_json:
                try:
                    reasons = json.loads(reasons_json)
                    # Look for the failure-signal reason specifically.
                    for r in reasons:
                        if "failure-signal" in r or "failure signal" in r:
                            most_recent = "debug intent with heavy shell activity, and a prior failure in this session"
                            break
                    else:
                        # Use the first reason as a generic fallback.
                        if reasons:
                            most_recent = str(reasons[-1])
                except Exception:
                    pass
    return count, most_recent


def status(
    verbose: bool = typer.Option(False, "--verbose", help="Show the underlying trace table."),
) -> None:
    """What does Hedwig think about this session right now?"""
    console = Console()
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)

    # Find the most recent session for this repo.
    with trust_db._connect() as conn:
        row = conn.execute(
            """
            SELECT session_id
            FROM decision_traces
            WHERE repo_root = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (repo_root_str,),
        ).fetchone()

    if row is None:
        # No sessions yet.
        body = Text()
        body.append(
            "Nothing to report yet — we haven't worked together in this repo.\n\n",
            style="white",
        )
        body.append(
            "Run ", style=PALETTE["meta"],
        )
        body.append("hw '<task>'", style=PALETTE["info_bold"])
        body.append(" to start.", style=PALETTE["meta"])
        console.print(
            Panel(
                body,
                title=panel_title("info", "status"),
                border_style=PALETTE["info"],
                padding=(1, 2),
            )
        )
        return

    session_id = row["session_id"]
    session_rows = trust_db.session_traces(repo_root_str, session_id)
    row_dicts = [dict(r) for r in session_rows]
    summary = summarize_session(row_dicts)

    # Session-scoped confirmed preferences (this session).
    session_prefs_raw = trust_db.confirmed_preferences_for_session(
        repo_root_str, session_id
    )
    session_prefs: list[LearnedPreference] = []
    for pref_row in session_prefs_raw:
        try:
            payload = json.loads(pref_row["preference_json"])
        except Exception:
            continue
        learned = _humanize_preference(payload, scope="this session")
        if learned is not None:
            session_prefs.append(learned)

    # Persistent preferences — accepted hypotheses from prior sessions.
    repo_prefs_raw = trust_db.confirmed_preferences_for_repo(repo_root_str)
    persistent_prefs: list[LearnedPreference] = []
    seen_drivers: set[str] = {pref_row.get("driver", "") for pref_row in session_prefs_raw}
    for pref_row in repo_prefs_raw:
        # Skip anything already shown as a session pref.
        driver = pref_row["driver"] or ""
        if pref_row["session_id"] == session_id:
            continue
        if driver in seen_drivers:
            continue
        try:
            payload = json.loads(pref_row["preference_json"])
        except Exception:
            continue
        learned = _humanize_preference(payload, scope="this repo")
        if learned is not None:
            persistent_prefs.append(learned)
            seen_drivers.add(driver)
        if len(persistent_prefs) >= 3:
            break

    # Proactive pauses + most-recent reason.
    proactive_count, most_recent_reason = _most_recent_proactive_reason(row_dicts)

    base_status = build_session_status(
        summary=summary,
        confirmed_session_preferences=tuple(session_prefs),
        persistent_preferences=tuple(persistent_prefs),
        most_recent_proactive_reason=most_recent_reason,
    )
    # Patch in proactive count (build_session_status defaults it to 0).
    from dataclasses import replace
    status_obj = replace(base_status, proactive_pauses=proactive_count)

    # Render as a single themed panel with prose lines inside.
    body = Text()

    # Opening sentence.
    body.append(template_session_sentence(status_obj), style="white")
    body.append("\n")

    # Proactive pause, if any.
    pause_line = template_proactive_pause_sentence(status_obj)
    if pause_line:
        body.append(pause_line, style=PALETTE["attention"])
        body.append("\n")

    # Learned preferences this session.
    if status_obj.session_preferences:
        body.append("\n")
        body.append(
            "What I've picked up in this session:\n",
            style=PALETTE["learn_bold"],
        )
        for pref in status_obj.session_preferences:
            body.append("  ")
            body.append(template_preference_line(pref) + "\n", style="white")

    # Nothing learned yet.
    if not status_obj.has_learned_anything and status_obj.turns_so_far > 0:
        body.append("\n")
        body.append(
            "I haven't inferred any preferences yet — it usually takes a few "
            "corrections in the same direction before I ask.",
            style=PALETTE["meta_italic"],
        )

    console.print(
        Panel(
            body,
            title=panel_title("info", "hedwig status"),
            border_style=PALETTE["info"],
            padding=(1, 2),
        )
    )

    if verbose:
        console.print()
        console.print(
            f"[{PALETTE['meta']}]— verbose —[/{PALETTE['meta']}]"
        )
        console.print(
            f"[{PALETTE['meta']}]session: {session_id[:12]}…  "
            f"turns: {summary.n_turns}  approvals: {summary.n_approvals}  "
            f"corrections: {summary.n_feedback}  denials: {summary.n_denials}  "
            f"failures: {summary.n_failures}[/{PALETTE['meta']}]"
        )
