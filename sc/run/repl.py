from __future__ import annotations

"""Hedwig REPL — persistent session loop.

`hw` (no subcommand) drops the developer into a long-running session where
tasks are submitted one by one. The banner shows once. The session_id and
classifier state persist across every task in the loop. This is the right
model for Hedwig's learning story: hypotheses fire after multiple tasks
accumulate signal, the scorer updates continuously, and the owl banner sits
at the top as a visual anchor.

Slash commands available inside the loop:
  /status     — what does Hedwig think about this session right now?
  /learning   — what has Hedwig learned about this repo?
  /prefs      — active confirmed preferences
  /intensity  — toggle active/delegating/auto
  /reset      — clear session state (keeps repo history)
  /exit       — exit the REPL

Entering a plain task runs it through the full governed pipeline.
"""

import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import typer
from rich import print
from rich.console import Console
from rich.prompt import Prompt
from rich.text import Text
from .intensity_toggle import run_toggle, OPTIONS as _OVERSIGHT_OPTIONS, _TO_INTENSITY, _label_from_intensity

from ..cli_shared import read_file_context as _read_file_context, resolve_config as _resolve_config
from ..config import SAConfig, autonomy_profile, config_dir
from ..repo import RepoError, get_repo_root
from ..session import ClaudeSession
from ..session_feedback import SessionFeedback
from ..trust_db import TrustDB
from .helpers import SpecContext, StudyContext, _load_spec_context
from .theme import PALETTE


_SLASH_COMMANDS = {
    "/status":   "What Hedwig thinks about this session",
    "/learning": "What Hedwig has learned about this repo",
    "/prefs":    "Active confirmed preferences",
    "/rules":    "Add or list rules  (/rules add <rule> | /rules list)",
    "/observe":  "Observe repo activity  (/observe report | traces | weights | personas)",
    "/oversight":"Set oversight level (hands-on / balanced / delegating)",
    "/reset":    "Clear session and start fresh",
    "/help":     "Show this list",
    "/exit":     "Exit Hedwig",
}


def _handle_slash(
    cmd: str,
    *,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    pinned_intensity: str | None,
) -> tuple[bool, str | None]:
    """Handle a slash command. Returns (should_continue, new_pinned_intensity)."""
    console = Console()
    parts = cmd.strip().split()
    verb = parts[0].lower()

    if verb == "/exit":
        return False, pinned_intensity

    if verb == "/help":
        for name, desc in _SLASH_COMMANDS.items():
            console.print(f"  [{PALETTE['info_bold']}]{name:<12}[/{PALETTE['info_bold']}] {desc}")
        return True, pinned_intensity

    if verb == "/status":
        from ..commands.status import status as _status_cmd
        try:
            _status_cmd(verbose=False)
        except SystemExit:
            pass
        return True, pinned_intensity

    if verb == "/learning":
        from ..commands.learning import learning as _learning_cmd
        try:
            _learning_cmd()
        except SystemExit:
            pass
        return True, pinned_intensity

    if verb == "/prefs":
        rows = trust_db.confirmed_preferences_for_repo(repo_root_str)
        accepted = [r for r in rows if json.loads(r["preference_json"]).get("accepted")]
        if not accepted:
            print(f"[{PALETTE['meta_italic']}]No confirmed preferences yet.[/{PALETTE['meta_italic']}]")
        else:
            from ..preferences import preference_from_dict
            from ..commands.status import _humanize_preference
            for r in accepted:
                payload = json.loads(r["preference_json"])
                learned = _humanize_preference(payload, scope="this repo")
                if learned:
                    print(f"  [{PALETTE['learn']}]✦[/{PALETTE['learn']}] {learned.headline}")
        return True, pinned_intensity

    if verb in ("/oversight", "/intensity"):
        if len(parts) >= 2:
            label = parts[1].lower()
            if label not in _OVERSIGHT_OPTIONS:
                current_label = _label_from_intensity(pinned_intensity)
                print(f"[{PALETTE['meta']}]current: {current_label}[/{PALETTE['meta']}]")
                for opt in _OVERSIGHT_OPTIONS:
                    from .intensity_toggle import _DESCRIPTIONS
                    marker = "◉" if opt == current_label else "○"
                    print(f"  [{PALETTE['meta']}]{marker}[/{PALETTE['meta']}] [{PALETTE['info_bold']}]{opt:<12}[/{PALETTE['info_bold']}]  [{PALETTE['meta']}]{_DESCRIPTIONS[opt]}[/{PALETTE['meta']}]")
                print(f"\n[{PALETTE['meta']}]usage: /oversight [balanced|hands-on|delegating][/{PALETTE['meta']}]")
                return True, pinned_intensity
            new_intensity = _TO_INTENSITY[label]
        else:
            # No arg — show numbered menu, read one keypress.
            from .intensity_toggle import _DESCRIPTIONS
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
                    add_rule(rule=rule_text, dry_run=False)
                except SystemExit:
                    pass
        else:
            print(f"[{PALETTE['meta']}]/rules list  or  /rules add <rule>[/{PALETTE['meta']}]")
        return True, pinned_intensity

    if verb == "/observe":
        sub = parts[1].lower() if len(parts) > 1 else "report"
        from ..commands.observe import report, traces, weights, personas, leases
        _obs_map = {
            "report":   lambda: report(json_out=False, verbose=False),
            "traces":   lambda: traces(limit=20, json_out=False),
            "weights":  lambda: weights(json_out=False),
            "personas": lambda: personas(limit=5, verbose=False),
            "leases":   lambda: leases(json_out=False),
        }
        fn = _obs_map.get(sub)
        if fn:
            try:
                fn()
            except SystemExit:
                pass
        else:
            print(f"[{PALETTE['meta']}]usage: /observe [report|traces|weights|personas|leases][/{PALETTE['meta']}]")
        return True, pinned_intensity

    if verb == "/reset":
        print(f"[{PALETTE['attention']}]Session cleared. Repo history and learned state preserved.[/{PALETTE['attention']}]")
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
    import json as _json
    _confirmed_count = sum(
        1 for r in _pref_rows
        if _json.loads(r["preference_json"]).get("accepted")
    )

    render_session_start_banner(
        config_model_id=config.model_id,
        profile_mode=profile.mode,
        prior_session_summary=_prior_summary,
        confirmed_pref_count=_confirmed_count,
    )

    print(f"[{PALETTE['meta']}]Type a task · /oversight to adjust · /help for commands.[/{PALETTE['meta']}]")
    print()

    # Single session_id shared across all tasks in this REPL session.
    run_session_id = uuid4().hex
    threshold = permanent_threshold if permanent_threshold is not None else config.permanent_approval_threshold
    client = ClaudeClient(model_id=config.model_id, region=config.aws_region)
    pinned_intensity: str | None = None  # None = auto-infer

    try:
        spec_context = _load_spec_context(repo_root, spec, config.read_max_chars)
    except FileNotFoundError as exc:
        print(f"[{PALETTE['deny']}]{exc}[/{PALETTE['deny']}]")
        raise typer.Exit(code=1)

    while True:
        # Print the prompt label with Rich markup, then read input directly.
        _console = Console()
        if pinned_intensity:
            _olabel = _label_from_intensity(pinned_intensity)
            _pin_color = PALETTE["learn"] if _olabel == "hands-on" else PALETTE["info"]
            _console.print(
                f"\n[{PALETTE['info_bold']}]hedwig[/{PALETTE['info_bold']}]"
                f" [{_pin_color}][{_olabel}][/{_pin_color}]",
                end=" ",
            )
        else:
            _console.print(f"\n[{PALETTE['info_bold']}]hedwig[/{PALETTE['info_bold']}]", end=" ")

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
        except Exception as exc:
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
            except Exception:
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
        except RuntimeError as exc:
            print(f"[{PALETTE['deny']}]{exc}[/{PALETTE['deny']}]")
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
                continue
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
