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
from ..preference_inference import infer_coding_mode, infer_user_persona, summarize_session
from ..run.theme import PALETTE, panel_title
from ..status import (
    LearnedPreference,
    build_session_status,
    template_preference_line,
    template_session_sentence,
    template_proactive_pause_sentence,
)


# Preference humanization now lives in sc/repo_memory.py (shared with the
# plugin's SessionStart hook). Thin alias preserves this module's callers.
from ..repo_memory import humanize_preference as _humanize_preference  # noqa: E402,F401


def _count_reviewer_calls(session_rows: list[dict]) -> int:
    """Count how many traces this session involved a second-opinion check
    (adversarial reviewer via model risk scoring)."""
    count = 0
    for row in session_rows:
        _found = False
        reasons_json = row.get("policy_reasons_json")
        if reasons_json:
            try:
                reasons = json.loads(reasons_json)
                if any("adversarial reviewer" in str(r).lower() for r in reasons):
                    _found = True
            except Exception:
                pass
        # Fallback: a non-null model_risk_score means the reviewer ran.
        if not _found and row.get("model_risk_score") is not None:
            _found = True
        if _found:
            count += 1
    return count


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

    # Find the most recent *live* session for this repo. Seeded demo traces
    # (session_id='seed_demo') are pre-history, not a real session — they
    # warm the classifier and hypothesis bank but should never appear as
    # "what's happening in this session right now."
    with trust_db._connect() as conn:
        row = conn.execute(
            """
            SELECT session_id
            FROM decision_traces
            WHERE repo_root = ? AND session_id != 'seed_demo'
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

    # Reviewer call count for this session.
    reviewer_calls = _count_reviewer_calls(row_dicts)

    # Learned model active?
    classifier = trust_db.load_policy_model(repo_root_str)
    if classifier is not None and classifier.ready():
        _model_line = (
            f"Decision model: learning from your decisions "
            f"({classifier.sample_count} real decisions)"
        )
        _model_style = PALETTE["learn_bold"]
    else:
        _sample_count = classifier.sample_count if classifier is not None else 0
        _model_line = (
            f"Decision model: using default rules "
            f"(need 10 real decisions to switch — {_sample_count} so far)"
        )
        _model_style = PALETTE["meta"]

    # Coding mode and engagement level.
    coding_mode = infer_coding_mode(summary)
    user_persona = infer_user_persona(summary)

    _coding_mode_labels = {
        "human_only": "human-authored",
        "collaborative": "collaborative",
        "vibe": "agent-led",
    }
    _persona_labels = {
        "active": "deep in it",
        "delegating": "delegating",
        "unknown": "unknown",
    }
    _coding_label = _coding_mode_labels.get(coding_mode.value, coding_mode.value)
    _persona_label = _persona_labels.get(user_persona.value, user_persona.value)

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

    # Session signals: coding mode + engagement level.
    body.append("\n")
    body.append(
        f"Authorship this session: {_coding_label}  |  "
        f"Your engagement level: {_persona_label}\n",
        style=PALETTE["meta"],
    )

    # Second-opinion checks.
    body.append(
        f"Second-opinion checks this run: {reviewer_calls}\n",
        style=PALETTE["meta"],
    )

    # Decision model status.
    body.append(_model_line + "\n", style=_model_style)

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
