from __future__ import annotations

"""Hedwig REPL — persistent session loop.

`hw` (no subcommand) drops into an interactive session. The banner shows once.
A single session_id is shared across all tasks in the loop, so behavioral
signals (pushback patterns, review timing, verification failures) accumulate
and the hypothesis bank and online classifier update continuously.

Slash commands (type /help for the full list):
  /prefs          Active confirmed preferences + pending hypotheses
  /context        What Hedwig pulled from repo memory for the last task
  /cochange       Files that historically change together in this repo
  /rules          List or add rules (/rules add <rule> | /rules list)
  /observe        Repo activity (report | traces | weights | personas | export)
  /oversight      Set oversight level (hands-on / balanced / delegating)
  /retrospective  Session wrap-up — where Hedwig was too loose or too cautious
  /new-session    Mark a session boundary (history is preserved)
  /help           Show this list
  /exit           Exit

Typing a plain task runs it through the full governed pipeline.

Terminology note: "oversight" is the user-facing label; "intensity" is the
internal variable name (active/delegating/None) used throughout the approval
cascade. The two map via _label_from_intensity() in oversight_toggle.py.
"""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import typer
from rich import print
from rich.console import Console
from rich.prompt import Prompt
from .oversight_toggle import OPTIONS as _OVERSIGHT_OPTIONS, _TO_INTENSITY, _label_from_intensity

from ..cli_shared import read_file_context as _read_file_context, resolve_config as _resolve_config
from ..config import autonomy_profile, config_dir
from ..repo import RepoError, get_repo_root
from ..session import ClaudeSession
from ..session_feedback import SessionFeedback
from ..trust_db import TrustDB
from .helpers import StudyContext, _load_spec_context
from .theme import PALETTE


_SLASH_SUBCOMMANDS = {
    "/observe": ["report", "traces", "weights", "personas", "leases", "export"],
    "/rules": ["add", "list"],
    "/oversight": ["hands-on", "balanced", "delegating"],
}


def _build_repl_session():
    """Lazy-build a prompt_toolkit PromptSession with slash-command completion.

    Falls back to ``None`` if prompt_toolkit isn't installed or the terminal
    doesn't support it (e.g. pipes, dumb TTYs); the caller will then use
    plain ``input()``.
    """
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import InMemoryHistory
    except Exception:
        return None

    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            parts = text.split(" ", 1)
            verb = parts[0]
            # First token: complete slash command names.
            if len(parts) == 1:
                for name in _SLASH_COMMANDS:
                    if name.startswith(verb):
                        yield Completion(
                            name,
                            start_position=-len(verb),
                            display_meta=_SLASH_COMMANDS[name],
                        )
                return
            # Subcommand position.
            subs = _SLASH_SUBCOMMANDS.get(verb)
            if not subs:
                return
            sub_prefix = parts[1].split(" ", 1)[0]
            for sub in subs:
                if sub.startswith(sub_prefix):
                    yield Completion(sub, start_position=-len(sub_prefix))

    try:
        return PromptSession(
            completer=_SlashCompleter(),
            history=InMemoryHistory(),
            complete_while_typing=True,
        )
    except Exception:
        return None


_SLASH_COMMANDS = {
    "/status":        "What Hedwig thinks about this session right now",
    "/prefs":         "Your saved preferences and patterns Hedwig is watching for",
    "/context":       "Show what Hedwig pulled from repo memory for the last task",
    "/cochange":      "Show files that historically change together in this repo",
    "/rules":         "Add or list rules  (/rules add <rule> | /rules list)",
    "/observe":       "Repo activity  (/observe report | traces | weights | personas | export [--html])",
    "/oversight":     "Set oversight level (hands-on / balanced / delegating)",
    "/retrospective": "Session wrap-up — where I was too loose or too cautious",
    "/new-session":   "Mark a session boundary (history is preserved)",
    "/doctor":        "Verify AWS identity and Bedrock connectivity",
    "/reset-demo":    "Clear all governance state (booth use — wipes trust.db)",
    "/seed-demo":     "Load hand-authored prior history (booth use — pre-warm classifier + hypothesis)",
    "/help":          "Show this list",
    "/exit":          "Exit Hedwig",
}


def _handle_slash(
    cmd: str,
    *,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    pinned_intensity: str | None,
    config=None,
) -> tuple[bool, str | None]:
    """Handle a slash command. Returns (should_continue, new_pinned_intensity)."""
    console = Console()
    parts = cmd.strip().split()
    verb = parts[0].lower()

    if verb == "/retrospective":
        from .retrospective import run_retrospective
        run_retrospective(
            trust_db=trust_db,
            repo_root_str=repo_root_str,
            session_id=run_session_id,
        )
        return True, pinned_intensity

    if verb == "/exit":
        # Offer retrospective on exit if the session had regrets — one line, easy to skip.
        from ..regret import detect_regret_events
        _rows = [dict(r) for r in trust_db.session_traces(repo_root_str, run_session_id)]
        if _rows:
            _regrets = detect_regret_events(_rows)
            if _regrets:
                console.print(
                    f"\n[{PALETTE['meta']}]{len(_regrets)} action{'s' if len(_regrets) != 1 else ''} "
                    f"could use a look — /retrospective before you go, or just exit.[/{PALETTE['meta']}]"
                )
        return False, pinned_intensity

    if verb == "/status":
        from ..commands.status import status as _status_cmd
        try:
            _status_cmd(verbose=False)
        except SystemExit:
            pass
        return True, pinned_intensity

    if verb == "/help":
        _w = max(len(n) for n in _SLASH_COMMANDS) + 2
        for name, desc in _SLASH_COMMANDS.items():
            console.print(f"  [{PALETTE['info_bold']}]{name:<{_w}}[/{PALETTE['info_bold']}] {desc}")
        return True, pinned_intensity

    if verb == "/prefs":
        import textwrap as _tw
        from rich.panel import Panel as _P
        from rich.text import Text as _T
        from .theme import panel_title as _pt
        from ..hypothesis_bank import MIN_EVIDENCE as _MIN_EVIDENCE
        _body = _T()

        # Accepted preferences.
        rows = trust_db.confirmed_preferences_for_repo(repo_root_str)
        accepted = [r for r in rows if json.loads(r["preference_json"]).get("accepted")]
        if accepted:
            _body.append("Accepted\n", style=PALETTE["learn_bold"])
            from ..commands.status import _humanize_preference
            for r in accepted:
                payload = json.loads(r["preference_json"])
                learned = _humanize_preference(payload, scope="this repo")
                if learned:
                    _body.append(f"  ✦ {learned.headline}\n", style=PALETTE["learn"])

        # Pending and rejected hypotheses — repo-scoped (not session-scoped),
        # so seeded hypotheses and prior-session candidates also surface here.
        with trust_db._connect() as _conn:
            _pending = _conn.execute(
                """SELECT prompt, rationale, evidence_for, evidence_against, status FROM hypothesis_candidates
                   WHERE repo_root = ? AND status IN ('pending', 'ready_to_surface')
                   ORDER BY (evidence_for + evidence_against) DESC, created_at ASC""",
                (repo_root_str,),
            ).fetchall()
            _rejected = _conn.execute(
                """SELECT prompt, evidence_for, evidence_against FROM hypothesis_candidates
                   WHERE repo_root = ? AND status = 'rejected'""",
                (repo_root_str,),
            ).fetchall()

        # Render each hypothesis in two visual rows:
        #   row 1: bar + pct  (left column, fixed width)
        #   row 2: prompt sentence, indented under the bar, wrapped softly.
        # This avoids Rich panel re-wrapping breaking a single-line layout.
        _PROMPT_WIDTH = 70
        _INDENT = " " * 4

        def _wrap_prompt(text: str) -> str:
            normalized = " ".join((text or "").split())
            return _tw.fill(
                normalized,
                width=_PROMPT_WIDTH,
                initial_indent=_INDENT,
                subsequent_indent=_INDENT,
            )

        def _bar_block(prompt_text: str, evf: int, rationale: str | None = None) -> None:
            progress = min(evf / _MIN_EVIDENCE, 1.0) if _MIN_EVIDENCE > 0 else 0.0
            filled = int(progress * 10)
            bar = "█" * filled + "░" * (10 - filled)
            _body.append(f"  {bar}  ", style=PALETTE["meta"])
            _body.append(f"{int(progress * 100):>3}%   ", style=PALETTE["info_bold"])
            _body.append(f"{evf}/{_MIN_EVIDENCE} traces\n", style=PALETTE["meta"])
            _body.append(f"{_wrap_prompt(prompt_text)}\n", style="white")
            if rationale:
                rsummary = " ".join(rationale.split())
                if len(rsummary) > 110:
                    rsummary = rsummary[:107] + "..."
                _body.append(f"{_INDENT}why: {rsummary}\n", style=PALETTE["meta_italic"])
            _body.append("\n")

        if _pending:
            if accepted:
                _body.append("\n")
            _body.append("Watching  ", style=PALETTE["info_bold"])
            _body.append(f"(evidence toward surfacing: {_MIN_EVIDENCE} traces needed)\n",
                         style=PALETTE["meta"])
            for c in _pending:
                _bar_block(c["prompt"], int(c["evidence_for"]), c["rationale"] if "rationale" in c.keys() else None)

        if _rejected:
            if accepted or _pending:
                _body.append("\n")
            _body.append("Set aside (not enough evidence yet)\n", style=PALETTE["meta"])
            for r in _rejected:
                evf = int(r["evidence_for"])
                total = evf + int(r["evidence_against"])
                _body.append(f"  ✗  {evf}/{total}   {_wrap_prompt(r['prompt'])}\n",
                             style=PALETTE["meta"])

        if not accepted and not _pending and not _rejected:
            _body.append("No preferences yet — patterns appear as you work.", style=PALETTE["meta_italic"])

        console.print(_P(_body, title=_pt("learn", "preferences"), border_style=PALETTE["learn"], padding=(1, 2)))
        return True, pinned_intensity

    if verb == "/context":
        from rich.panel import Panel as _P
        from rich.text import Text as _T
        from .theme import panel_title as _pt
        try:
            from . import context_capture as _cc
            last = _cc.last()
        except Exception:
            console.print(f"[{PALETTE['meta']}]Context unavailable.[/{PALETTE['meta']}]")
            return True, pinned_intensity
        body = _T()
        if last.total() == 0 and not last.summary:
            body.append("No task run yet this session — context shows up after the first run.",
                        style=PALETTE["meta_italic"])
        else:
            if last.task_text:
                snippet = " ".join(last.task_text.split())
                if len(snippet) > 90:
                    snippet = snippet[:87] + "..."
                body.append("For task: ", style=PALETTE["meta"])
                body.append(f"{snippet}\n\n", style="white")

            if last.summary:
                import textwrap as _tw2
                wrapped = _tw2.fill(last.summary, width=78, initial_indent="  ", subsequent_indent="  ")
                body.append("What we've learned about this repo\n", style=PALETTE["info_bold"])
                body.append(f"{wrapped}\n\n", style="white")

            def _section(title: str, items: list[str]) -> None:
                if not items:
                    return
                body.append(f"{title}\n", style=PALETTE["info_bold"])
                for it in items:
                    text = " ".join((it or "").split())
                    if len(text) > 100:
                        text = text[:97] + "..."
                    body.append(f"  • {text}\n", style="white")
                body.append("\n")

            _section(f"Repo notes ({len(last.logic_notes)})", last.logic_notes)
            _section(f"Behavioral guidelines ({len(last.guidelines)})", last.guidelines)
            _section(f"Past developer feedback ({len(last.feedback)})", last.feedback)

            body.append("Ranked by keyword overlap with the task.", style=PALETTE["meta_italic"])

        console.print(_P(body, title=_pt("info", "context retrieved"),
                         border_style=PALETTE["info"], padding=(1, 2)))
        return True, pinned_intensity

    if verb == "/cochange":
        from rich.panel import Panel as _P
        from rich.text import Text as _T
        from .theme import panel_title as _pt
        from ..cochange import cochange_graph

        try:
            graph = cochange_graph(trust_db, repo_root_str, min_count=2, limit_per_file=3)
        except Exception:
            graph = {}
        body = _T()
        if not graph:
            body.append(
                "No co-change history yet — patterns appear as you edit files together across sessions.",
                style=PALETTE["meta_italic"],
            )
        else:
            body.append("Files that have moved together across sessions\n\n", style=PALETTE["info_bold"])
            for src in sorted(graph.keys()):
                body.append(f"  {src}\n", style="white")
                for nbr, n in graph[src]:
                    body.append(f"    └─ {nbr}  ", style=PALETTE["meta"])
                    body.append(f"({n} session{'s' if n != 1 else ''})\n", style=PALETTE["meta"])
                body.append("\n")
            body.append(
                "Surfaced at plan stage when you edit a file with co-change history.",
                style=PALETTE["meta_italic"],
            )
        console.print(_P(body, title=_pt("info", "co-change graph"),
                         border_style=PALETTE["info"], padding=(1, 2)))
        return True, pinned_intensity

    if verb in ("/oversight", "/intensity"):
        if len(parts) >= 2:
            label = parts[1].lower()
            if label not in _OVERSIGHT_OPTIONS:
                current_label = _label_from_intensity(pinned_intensity)
                print(f"[{PALETTE['meta']}]current: {current_label}[/{PALETTE['meta']}]")
                for opt in _OVERSIGHT_OPTIONS:
                    from .oversight_toggle import _DESCRIPTIONS
                    marker = "◉" if opt == current_label else "○"
                    print(f"  [{PALETTE['meta']}]{marker}[/{PALETTE['meta']}] [{PALETTE['info_bold']}]{opt:<12}[/{PALETTE['info_bold']}]  [{PALETTE['meta']}]{_DESCRIPTIONS[opt]}[/{PALETTE['meta']}]")
                print(f"\n[{PALETTE['meta']}]usage: /oversight [balanced|hands-on|delegating][/{PALETTE['meta']}]")
                return True, pinned_intensity
            new_intensity = _TO_INTENSITY[label]
        else:
            # No arg — show numbered menu, read one keypress.
            from .oversight_toggle import _DESCRIPTIONS
            current_label = _label_from_intensity(pinned_intensity)
            print()
            for i, opt in enumerate(_OVERSIGHT_OPTIONS, 1):
                marker = "◉" if opt == current_label else "○"
                _oc = PALETTE["learn"] if opt == "hands-on" else PALETTE["info"] if opt == "delegating" else PALETTE["meta"]
                print(f"  [{PALETTE['meta']}]{i}. {marker}[/{PALETTE['meta']}] [{_oc}]{opt:<12}[/{_oc}]  [{PALETTE['meta']}]{_DESCRIPTIONS[opt]}[/{PALETTE['meta']}]")
            print()
            try:
                pick = input("  select [1-3] or Enter to keep: ").strip()
            except (KeyboardInterrupt, EOFError):
                return True, pinned_intensity
            if pick in ("1", "2", "3"):
                label = _OVERSIGHT_OPTIONS[int(pick) - 1]
                new_intensity = _TO_INTENSITY[label]
                if new_intensity != pinned_intensity:
                    _oc = PALETTE["learn"] if label == "hands-on" else PALETTE["info"] if label == "delegating" else PALETTE["meta"]
                    print(f"[{_oc}]⊙ {label}[/{_oc}]")
                return True, new_intensity
            return True, pinned_intensity
        if new_intensity != pinned_intensity:
            label = _label_from_intensity(new_intensity)
            _color = PALETTE["learn"] if label == "hands-on" else PALETTE["info"] if label == "delegating" else PALETTE["meta"]
            print(f"[{_color}]⊙ {label}[/{_color}]")
        return True, new_intensity

    if verb == "/rules":
        sub = parts[1].lower() if len(parts) > 1 else "list"
        if sub == "list":
            from ..commands.admin import rules_list
            try:
                rules_list(json_out=False)
            except SystemExit:
                pass
        elif sub == "add":
            rule_text = " ".join(parts[2:])
            if not rule_text:
                print(f"[{PALETTE['meta']}]usage: /rules add <natural language rule>[/{PALETTE['meta']}]")
            else:
                from ..commands.admin import add_rule
                try:
                    add_rule(rule=rule_text, source="manual_rule", model_id=None, region=None, yes=True)
                except SystemExit:
                    pass
        else:
            print(f"[{PALETTE['meta']}]/rules list  or  /rules add <rule>[/{PALETTE['meta']}]")
        return True, pinned_intensity

    if verb == "/observe":
        from ..commands.observe import (
            report, traces, weights, personas, leases, export,
        )
        # Accept multiple subcommands on the same line ("/observe report export
        # --html") by scanning all args; last-known-subcommand wins. Also
        # tolerate -html as an alias for --html since visitors mistype.
        known = {"report", "traces", "weights", "personas", "leases", "export"}
        sub = "report"
        for p in parts[1:]:
            if p.lower() in known:
                sub = p.lower()
        html_flag = any(p in ("--html", "-html") for p in parts)
        _obs_map = {
            "report":      lambda: report(json_out=False, verbose=False),
            "traces":      lambda: traces(limit=20, json_out=False),
            "weights":     lambda: weights(verbose=False),
            "personas":    lambda: personas(limit=5, verbose=False),
            "leases":      lambda: leases(json_out=False),
            "export":      lambda: export(out=Path(".sc/exports"), session_id=None, html_report=html_flag, open_browser=True),
        }
        fn = _obs_map.get(sub)
        if fn:
            try:
                fn()
            except SystemExit:
                pass
        else:
            print(f"[{PALETTE['meta']}]usage: /observe [report|traces|weights|personas|leases|export [--html]][/{PALETTE['meta']}]")
        return True, pinned_intensity

    if verb == "/new-session":
        print(f"[{PALETTE['attention']}]Session cleared. Repo history and learned state preserved.[/{PALETTE['attention']}]")
        return True, pinned_intensity

    if verb == "/doctor":
        from ..commands.admin import doctor as _doctor_cmd
        # _doctor_cmd's signature uses typer.Option defaults; calling it
        # directly leaks OptionInfo objects. Pass real values from config.
        try:
            _doctor_cmd(
                model_id=getattr(config, "model_id", None),
                region=getattr(config, "aws_region", None),
                prompt="Say OK and nothing else.",
            )
        except SystemExit:
            pass
        return True, pinned_intensity

    if verb == "/reset-demo":
        # Booth-only: wipe all per-repo governance state so the next visitor
        # starts cold. Keeps the file on disk; just clears every table.
        try:
            confirm = Prompt.ask(
                f"[{PALETTE['attention_bold']}]Wipe all governance state for this repo?[/{PALETTE['attention_bold']}]"
                f" [{PALETTE['meta']}](traces, leases, prefs, hypotheses, model)[/{PALETTE['meta']}]",
                choices=["y", "n"],
                default="n",
            )
        except (KeyboardInterrupt, EOFError):
            print(f"\n[{PALETTE['meta']}]cancelled.[/{PALETTE['meta']}]")
            return True, pinned_intensity
        if confirm != "y":
            print(f"[{PALETTE['meta']}]cancelled.[/{PALETTE['meta']}]")
            return True, pinned_intensity
        with trust_db._connect() as _conn:
            for _table in (
                "decisions", "decision_traces", "leases", "read_leases",
                "autonomy_preferences", "confirmed_preferences",
                "hypothesis_candidates", "policy_models", "policy_model_snapshots",
                # Added 2026-05-25: these tables also accumulate per-repo
                # learned state. Without wiping them, plan revisions,
                # learned guidelines, and any /rules add from the previous
                # booth visitor leak into the next visitor's session.
                "plan_revisions", "logic_notes", "behavioral_guidelines",
                "hard_constraints",
            ):
                try:
                    _conn.execute(f"DELETE FROM {_table} WHERE repo_root = ?", (repo_root_str,))
                except Exception:
                    pass
        # Restore demo fixture files so the task "add a search-by-tag method"
        # always has something real to add. Without this, code written by a
        # prior demo run accumulates and the model finds nothing to do.
        import subprocess as _sp
        _demo_dir = str(Path(repo_root_str) / "demo_recipe_api")
        try:
            _sp.run(
                ["git", "restore",
                 "demo_recipe_api/recipe_api/store.py",
                 "demo_recipe_api/recipe_api/service.py",
                 "demo_recipe_api/tests/test_api.py"],
                cwd=repo_root_str, capture_output=True,
            )
            # Do NOT use git clean here — test_store.py is untracked and
            # would be permanently deleted on every reset.
            _sp.run(
                ["git", "restore", "demo_recipe_api/tests/test_api.py"],
                cwd=repo_root_str, capture_output=True,
            )
        except Exception:
            pass
        print(f"[{PALETTE['approve_bold']}]✓ governance state cleared — ready for next visitor.[/{PALETTE['approve_bold']}]")
        return True, pinned_intensity

    if verb == "/seed-demo":
        # Booth-only: load a hand-authored prior-history bundle so the
        # classifier is past MIN_SAMPLES_FOR_LEARNED and one hypothesis is
        # near surface threshold. Tagged session_id='seed_demo' so /observe
        # and the HTML export can separate seeded from live activity.
        from ..demo_seed import seed_demo, SEED_SESSION_ID
        try:
            result = seed_demo(trust_db, repo_root_str)
        except Exception as exc:
            print(f"[{PALETTE['deny']}]seed failed: {exc}[/{PALETTE['deny']}]")
            return True, pinned_intensity
        if result["already_seeded"]:
            print(
                f"[{PALETTE['meta']}]already seeded — run /reset-demo first if you want to reseed.[/{PALETTE['meta']}]"
            )
            return True, pinned_intensity
        print(
            f"[{PALETTE['approve_bold']}]✓ seeded[/{PALETTE['approve_bold']}]"
            f"  [{PALETTE['meta']}]· {result['traces']} traces · "
            f"classifier pre-warmed ({result['updates']} updates) · "
            f"1 hypothesis at evidence 2/3 · session_id={SEED_SESSION_ID}[/{PALETTE['meta']}]"
        )
        return True, pinned_intensity

    print(f"[{PALETTE['deny']}]Unknown command '{verb}'. Type /help for a list.[/{PALETTE['deny']}]")
    return True, pinned_intensity


def run_repl(
    *,
    model_id: str | None = None,
    region: str | None = None,
    remember: bool = True,
    show_intent: bool = False,
    show_system_prompt: bool = False,
    spec: str | None = None,
    permanent_threshold: int | None = None,
) -> None:
    """Start the Hedwig REPL. Banner shows once; tasks run in a shared session."""
    from ..agent_client import ClaudeClient
    from ..preference_inference import summarize_session
    from .banner import render_session_start_banner
    from .command import (
        _apply_updates_and_verify,
        _capture_logic_notes,
        _evaluate_apply_stage,
        _record_declare_stage,
        _resolve_intent_declaration,
    )
    from .model import _generate_updates_with_repair
    from .ui import _confirm_create_files, _render_intent_summary
    from ..prompt_builder import build_run_system_prompt
    import threading

    console = Console()

    try:
        repo_root = get_repo_root()
    except RepoError as exc:
        print(f"[{PALETTE['deny']}]{exc}[/{PALETTE['deny']}]")
        raise typer.Exit(code=1)

    try:
        config = _resolve_config(repo_root, model_id, region)
    except typer.BadParameter as exc:
        print(f"[{PALETTE['deny']}]{exc}[/{PALETTE['deny']}]")
        raise typer.Exit(code=1)

    trust_db = TrustDB(config_dir(repo_root) / "trust.db")
    repo_root_str = str(repo_root)
    profile = autonomy_profile(config)
    study_context = StudyContext(
        participant_id=None,
        study_run_id=None,
        study_task_id=None,
        autonomy_mode=profile.mode,
    )

    # Prior session summary for the banner — same logic as current run command.
    with trust_db._connect() as _conn:
        _prior_rows = _conn.execute(
            """
            SELECT session_id, user_decision, edit_distance, user_feedback_text,
                   task, pushback_type, created_at
            FROM decision_traces WHERE repo_root = ?
            ORDER BY created_at DESC LIMIT 100
            """,
            (repo_root_str,),
        ).fetchall()
    _prior_summary = summarize_session([dict(r) for r in _prior_rows]) if _prior_rows else None

    # Count confirmed preferences for the banner.
    _pref_rows = trust_db.confirmed_preferences_for_repo(repo_root_str)
    _confirmed_count = sum(
        1 for r in _pref_rows
        if json.loads(r["preference_json"]).get("accepted")
    )

    render_session_start_banner(
        config_model_id=config.model_id,
        profile_mode=profile.mode,
        prior_session_summary=_prior_summary,
        confirmed_pref_count=_confirmed_count,
    )

    # On cold-start (no prior sessions), show a one-line orientation so a
    # first-time user immediately understands what Hedwig does.
    if _prior_summary is None or _prior_summary.n_turns == 0:
        print(
            f"[{PALETTE['info']}]Hedwig governs each file change — "
            f"you approve, deny, or teach.[/{PALETTE['info']}]"
        )
    print(f"[{PALETTE['meta']}]Type a task · /oversight to adjust · /help for commands.[/{PALETTE['meta']}]")
    print()

    # Single session_id shared across all tasks in this REPL session.
    run_session_id = uuid4().hex
    threshold = permanent_threshold if permanent_threshold is not None else config.permanent_approval_threshold
    client = ClaudeClient(model_id=config.model_id, region=config.aws_region)
    pinned_intensity: str | None = None  # None = auto-infer
    pt_session = _build_repl_session()

    try:
        spec_context = _load_spec_context(repo_root, spec, config.read_max_chars)
    except FileNotFoundError as exc:
        print(f"[{PALETTE['deny']}]{exc}[/{PALETTE['deny']}]")
        raise typer.Exit(code=1)

    while True:
        # When prompt_toolkit is available, render the label inline as part
        # of the pt prompt so its completion menu lays out cleanly.
        if pt_session is not None:
            from prompt_toolkit.formatted_text import ANSI
            # ANSI escapes: \x1b[1;36m = bold cyan, \x1b[2;37m = dim, \x1b[0m = reset.
            # Matches the Rich-styled label used by the fallback path.
            _BOLD_CYAN = "\x1b[1;36m"
            _DIM = "\x1b[2;37m"
            _RESET = "\x1b[0m"
            if pinned_intensity:
                _olabel = _label_from_intensity(pinned_intensity)
                label = f"\n{_BOLD_CYAN}hedwig{_RESET} {_DIM}[{_olabel}]{_RESET} "
            else:
                label = f"\n{_BOLD_CYAN}hedwig{_RESET} "
            try:
                raw = pt_session.prompt(ANSI(label)).strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n[{PALETTE['meta']}]goodbye.[/{PALETTE['meta']}]")
                break
        else:
            # Fallback: Rich markup label + plain input().
            if pinned_intensity:
                _olabel = _label_from_intensity(pinned_intensity)
                _pin_color = (
                    PALETTE["learn"] if _olabel == "hands-on"
                    else PALETTE["info"] if _olabel == "delegating"
                    else PALETTE["meta"]
                )
                console.print(
                    f"\n[{PALETTE['info_bold']}]hedwig[/{PALETTE['info_bold']}]"
                    f" [{_pin_color}][{_olabel}][/{_pin_color}]",
                    end=" ",
                )
            else:
                console.print(f"\n[{PALETTE['info_bold']}]hedwig[/{PALETTE['info_bold']}]", end=" ")
            try:
                raw = input().strip()
            except (KeyboardInterrupt, EOFError):
                print(f"\n[{PALETTE['meta']}]goodbye.[/{PALETTE['meta']}]")
                break

        if not raw:
            continue

        if raw == "/":
            raw = "/help"
        if raw.startswith("/"):
            should_continue, pinned_intensity = _handle_slash(
                raw,
                trust_db=trust_db,
                repo_root_str=repo_root_str,
                run_session_id=run_session_id,
                pinned_intensity=pinned_intensity,
                config=config,
            )
            if not should_continue:
                print(f"[{PALETTE['meta']}]goodbye.[/{PALETTE['meta']}]")
                break
            continue

        task = raw
        current_phase = "planning"
        feedback = SessionFeedback(current_phase=current_phase)

        session = ClaudeSession(
            build_run_system_prompt(
                trust_db=trust_db,
                repo_root=repo_root_str,
                workflow_phase=current_phase,
                autonomy_mode=profile.mode,
                task_text=task,
                spec_digest=spec_context.digest if spec_context else None,
            )
        )
        if show_system_prompt:
            from .ui import _show_system_prompt
            _show_system_prompt(current_phase, session.system_prompt)

        try:
            resolution = _resolve_intent_declaration(
                client=client,
                session=session,
                task=task,
                config=config,
                trust_db=trust_db,
                repo_root=repo_root,
                repo_root_str=repo_root_str,
                run_session_id=run_session_id,
                current_phase=current_phase,
                show_system_prompt=show_system_prompt,
                feedback=feedback,
                study_context=study_context,
                spec_context=spec_context,
            )
        except typer.Exit:
            continue
        except KeyboardInterrupt:
            print(f"\n[{PALETTE['meta']}]cancelled.[/{PALETTE['meta']}]")
            continue
        except (ValueError, RuntimeError, KeyError, AttributeError) as exc:
            print(f"[{PALETTE['deny']}]Error during planning: {exc}[/{PALETTE['deny']}]")
            continue

        declaration = resolution.declaration
        current_phase = resolution.current_phase
        planned_files = declaration.planned_files
        _record_declare_stage(
            trust_db=trust_db,
            repo_root_str=repo_root_str,
            run_session_id=run_session_id,
            task=task,
            declaration=declaration,
            study_context=study_context,
        )
        if show_intent and not resolution.intent_rendered_during_checkpoint:
            _render_intent_summary(declaration)

        file_context = _read_file_context(repo_root, planned_files, config.read_max_chars)
        file_hashes = {}
        for path in planned_files:
            try:
                current = (repo_root / path).read_text()
            except (FileNotFoundError, IsADirectoryError, OSError):
                current = ""
            file_hashes[path] = hashlib.sha256(current.encode("utf-8")).hexdigest()

        try:
            updates, patch_text, touched_files = _generate_updates_with_repair(
                client=client,
                session=session,
                declaration=declaration,
                file_context=file_context,
                allowed_files=set(planned_files),
                repo_root=repo_root,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                task=task,
                session_id=run_session_id,
                trust_db=trust_db,
                repo_root_str=repo_root_str,
                current_phase=resolution.current_phase,
                show_system_prompt=show_system_prompt,
                feedback=feedback,
                autonomy_mode=profile.mode,
                study_context=study_context,
                spec_context=spec_context,
            )
        except typer.Exit:
            continue
        except KeyboardInterrupt:
            print(f"\n[{PALETTE['meta']}]cancelled.[/{PALETTE['meta']}]")
            continue
        except RuntimeError as exc:
            print(f"[{PALETTE['deny']}]{exc}[/{PALETTE['deny']}]")
            continue

        if not touched_files:
            # No-op task (already-complete signal from generate_updates). The
            # ✓ banner was already printed; surface any ready hypothesis then
            # loop back to the prompt.
            try:
                from .apply_stage import _surface_ready_hypothesis_after_no_op
                _surface_ready_hypothesis_after_no_op(
                    trust_db=trust_db,
                    repo_root_str=repo_root_str,
                    run_session_id=run_session_id,
                )
            except Exception:
                pass
            continue

        new_files = [p for p in touched_files if not (repo_root / p).exists()]
        if new_files and not _confirm_create_files(new_files):
            print(f"[{PALETTE['attention']}]Patch denied.[/{PALETTE['attention']}]")
            continue

        # Apply intensity override if pinned.
        _intensity_override = pinned_intensity

        try:
            _evaluate_apply_stage(
                repo_root=repo_root,
                config=config,
                trust_db=trust_db,
                repo_root_str=repo_root_str,
                run_session_id=run_session_id,
                task=task,
                session=session,
                feedback=feedback,
                updates=updates,
                touched_files=touched_files,
                declaration=declaration,
                planned_files=planned_files,
                remember=remember,
                threshold=threshold,
                client=client,
                study_context=study_context,
                session_intensity_override=_intensity_override,
            )
        except typer.Exit as exc:
            if exc.exit_code != 0:
                raise  # hard constraint deny — propagate so the REPL doesn't silently swallow it
            continue  # soft deny (code=0) — loop back to prompt
        except KeyboardInterrupt:
            # Ctrl+C inside an apply prompt — treat as deny + return to REPL
            # rather than letting it tear down the whole session.
            print(f"\n[{PALETTE['meta']}]cancelled — apply skipped.[/{PALETTE['meta']}]")
            continue

        _apply_updates_and_verify(
            repo_root=repo_root,
            config=config,
            trust_db=trust_db,
            repo_root_str=repo_root_str,
            run_session_id=run_session_id,
            declaration=declaration,
            updates=updates,
            touched_files=touched_files,
            file_hashes=file_hashes,
        )

        threading.Thread(
            target=_capture_logic_notes,
            kwargs=dict(
                trust_db=trust_db,
                repo_root=repo_root_str,
                session_id=run_session_id,
                task=task,
                declaration=declaration,
                touched_files=touched_files,
                patch_text=patch_text,
                spec_context=spec_context,
                client=client,
            ),
            daemon=True,
        ).start()

        print(f"[{PALETTE['approve_bold']}]✓ done[/{PALETTE['approve_bold']}]")
