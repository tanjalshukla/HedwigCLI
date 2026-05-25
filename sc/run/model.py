from __future__ import annotations

"""Model interaction helpers for run flow (check-ins, phase changes, update retries)."""

import textwrap
import time
from pathlib import Path

import typer
from rich import print
from rich.prompt import Prompt

from ..agent_client import ClaudeClient, ModelCheckInRequired
from ..plan_gate import evaluate_write_phase_gate
from .helpers import PatchValidationError, validate_touched_files
from ..prompt_builder import build_run_system_prompt
from ..schema import CheckInMessage, IntentDeclaration, WorkflowPhase
from ..session import ClaudeSession
from ..session_feedback import SessionFeedback
from ..trust_db import TrustDB
from .helpers import SpecContext, StudyContext, _apply_feedback_learning, _build_patch_from_updates
from .ui import _model_status, _show_system_prompt


def _refresh_session_context(session: ClaudeSession, feedback: SessionFeedback) -> None:
    session.set_session_context(feedback.build_and_consume_context())


def _infer_phase_from_checkin(check_in: CheckInMessage, current_phase: WorkflowPhase) -> WorkflowPhase:
    content = f"{check_in.reason} {check_in.content}".lower()
    if "implement" in content:
        return "implementation"
    if "review" in content or "test" in content:
        return "review"
    if "research" in content:
        return "research"
    if check_in.check_in_type in {"plan_review", "decision_point", "deviation_notice"}:
        return "planning"
    return current_phase


def _apply_phase_transition_with_display(
    *,
    session: ClaudeSession,
    trust_db: TrustDB,
    repo_root: str,
    current_phase: WorkflowPhase,
    next_phase: WorkflowPhase,
    show_system_prompt: bool,
    feedback: SessionFeedback,
    autonomy_mode: str,
    task_text: str,
    spec_digest: str | None = None,
) -> WorkflowPhase:
    phase_changed = next_phase != current_phase
    if phase_changed:
        session.system_prompt = build_run_system_prompt(
            trust_db=trust_db,
            repo_root=repo_root,
            workflow_phase=next_phase,
            autonomy_mode=autonomy_mode,
            task_text=task_text,
            spec_digest=spec_digest,
        )
        current_phase = next_phase
    feedback.set_phase(current_phase)
    if show_system_prompt and phase_changed:
        _show_system_prompt(current_phase, session.system_prompt)
    return current_phase


def _handle_model_checkin(
    *,
    check_in: CheckInMessage,
    stage: str,
    task: str,
    session_id: str,
    trust_db: TrustDB,
    repo_root_str: str,
    session: ClaudeSession,
    feedback: SessionFeedback,
    client: ClaudeClient | None = None,
    study_context: StudyContext | None = None,
) -> tuple[bool, str, str | None]:
    from ..run.theme import PALETTE, panel_title
    print()
    print(panel_title("approve_request", stage))
    print(f"[{PALETTE['meta']}]{check_in.reason}[/{PALETTE['meta']}]")
    if check_in.recommendation:
        print(f"[{PALETTE['meta']}]{check_in.recommendation}[/{PALETTE['meta']}]")

    response_text = ""
    prompt_started = time.time()
    approved = False

    # Only surface options when they represent genuinely different tradeoffs.
    # Cap at 2 options — the model sometimes generates redundant ones.
    meaningful_options = check_in.options[:2] if check_in.options else []
    if len(meaningful_options) >= 2:
        print()
        for idx, option in enumerate(meaningful_options, 1):
            display_option = textwrap.shorten(" ".join(option.split()), width=120, placeholder="...")
            print(f"  [{PALETTE['meta']}]{idx}.[/{PALETTE['meta']}] {display_option}")
        choices = [str(i) for i in range(1, len(meaningful_options) + 1)] + ["d"]
        pick = Prompt.ask(
            f"[{PALETTE['approve_bold']}]1[/{PALETTE['approve_bold']}]-{len(meaningful_options)} choose  [{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny",
            choices=choices,
            default=choices[0],
        )
        if pick == "d":
            approved = False
        else:
            approved = True
            response_text = meaningful_options[int(pick) - 1]
    else:
        pick = Prompt.ask(
            f"[{PALETTE['approve_bold']}]a[/{PALETTE['approve_bold']}] proceed  [{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny",
            choices=["a", "d"],
            default="a",
        )
        approved = pick != "d"
        if approved:
            response_text = check_in.recommendation or "Proceed with current approach."

    # No "Optional architectural guidance" prompt — it interrupts flow and is
    # rarely used. Developers who want to steer use the deny path.
    captured_feedback: str | None = (
        response_text if approved and response_text != "Proceed with current approach." else None
    )
    if captured_feedback:
        _apply_feedback_learning(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session=session,
            feedback_text=captured_feedback,
            client=client,
            guidance_prefix="Developer guidance",
        )

    response_time_ms = int((time.time() - prompt_started) * 1000)
    trust_db.record_decision(
        repo_root_str,
        task,
        "check_in",
        approved=approved,
        remembered=False,
        planned_files=[],
    )
    trust_db.record_trace(
        repo_root=repo_root_str,
        session_id=session_id,
        task=task,
        stage=stage,
        action_type="check_in",
        file_path="__session__",
        change_type=check_in.check_in_type,
        diff_size=None,
        blast_radius=None,
        existing_lease=False,
        lease_type=None,
        prior_approvals=0,
        prior_denials=0,
        policy_action="check_in",
        policy_score=0.0,
        user_decision="approve" if approved else "deny",
        response_time_ms=response_time_ms,
        edit_distance=None,
        user_feedback_text=captured_feedback,
        model_confidence_self_report=check_in.confidence,
        model_assumptions=check_in.assumptions,
        check_in_initiator="model_proactive",
        participant_id=study_context.participant_id if study_context else None,
        study_run_id=study_context.study_run_id if study_context else None,
        study_task_id=study_context.study_task_id if study_context else None,
        autonomy_mode=study_context.autonomy_mode if study_context else None,
    )
    if not approved:
        feedback.note_decision(
            False,
            change_patterns=[check_in.check_in_type],
            response_time_ms=response_time_ms,
            feedback_text=captured_feedback,
        )
        return False, "", captured_feedback

    feedback.note_decision(
        True,
        response_time_ms=response_time_ms,
        feedback_text=captured_feedback,
    )
    session.add_user(f"Developer check-in response: {response_text}")
    return True, response_text, captured_feedback


def _generate_updates_with_repair(
    *,
    client: ClaudeClient,
    session: ClaudeSession,
    declaration: IntentDeclaration,
    file_context: dict[str, str],
    allowed_files: set[str],
    repo_root: Path,
    max_tokens: int,
    temperature: float,
    task: str,
    session_id: str,
    trust_db: TrustDB,
    repo_root_str: str,
    current_phase: WorkflowPhase,
    show_system_prompt: bool,
    feedback: SessionFeedback,
    autonomy_mode: str,
    study_context: StudyContext | None = None,
    spec_context: SpecContext | None = None,
) -> tuple[dict[str, str], str, list[str]]:
    update_error: str | None = None
    max_update_attempts = 3
    max_model_checkins = 5
    update_attempt = 0
    model_checkins = 0
    _last_checkin_type: str | None = None  # deduplication guard

    while update_attempt < max_update_attempts:
        try:
            session.system_prompt = build_run_system_prompt(
                trust_db=trust_db,
                repo_root=repo_root_str,
                workflow_phase=current_phase,
                autonomy_mode=autonomy_mode,
                task_text=task,
                spec_digest=spec_context.digest if spec_context else None,
            )
            _refresh_session_context(session, feedback)
            # Open with file context — tells the viewer specifically which
            # files Hedwig is about to edit.
            _planned = list(declaration.planned_files or [])
            if _planned:
                if len(_planned) <= 2:
                    _initial = f"drafting edits to {', '.join(_planned)}"
                else:
                    _initial = f"drafting edits to {_planned[0]} and {len(_planned) - 1} more"
            else:
                _initial = "drafting edits"
            with _model_status("updates", initial_thought=_initial):
                updates = client.generate_updates(
                    session,
                    declaration,
                    file_context=file_context,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    repair_hint=update_error,
                )
        except ModelCheckInRequired as exc:
            # Deduplicate: if the model sends the same check-in type twice in a row
            # (e.g. two consecutive phase_transition check-ins), auto-approve the
            # second one silently rather than prompting again.
            if exc.message.check_in_type == _last_checkin_type:
                _last_checkin_type = exc.message.check_in_type
                next_phase = _infer_phase_from_checkin(exc.message, current_phase)
                current_phase = _apply_phase_transition_with_display(
                    session=session,
                    trust_db=trust_db,
                    repo_root=repo_root_str,
                    current_phase=current_phase,
                    next_phase=next_phase,
                    show_system_prompt=show_system_prompt,
                    feedback=feedback,
                    autonomy_mode=autonomy_mode,
                    task_text=task,
                    spec_digest=spec_context.digest if spec_context else None,
                )
                session.add_user("Proceed with current approach.")
                continue
            _last_checkin_type = exc.message.check_in_type
            model_checkins += 1
            if model_checkins > max_model_checkins:
                update_error = "Too many model check-ins during implementation."
                break
            approved, _, _ = _handle_model_checkin(
                check_in=exc.message,
                stage="implementation",
                task=task,
                session_id=session_id,
                trust_db=trust_db,
                repo_root_str=repo_root_str,
                session=session,
                feedback=feedback,
                client=client,
                study_context=study_context,
            )
            if not approved:
                from ..run.theme import PALETTE as _PAL
                print(f"[{_PAL['attention']}]Task denied during model check-in.[/{_PAL['attention']}]")
                raise typer.Exit(code=0)
            next_phase = _infer_phase_from_checkin(exc.message, current_phase)
            current_phase = _apply_phase_transition_with_display(
                session=session,
                trust_db=trust_db,
                repo_root=repo_root_str,
                current_phase=current_phase,
                next_phase=next_phase,
                show_system_prompt=show_system_prompt,
                feedback=feedback,
                autonomy_mode=autonomy_mode,
                task_text=task,
                spec_digest=spec_context.digest if spec_context else None,
            )
            continue
        except Exception as exc:
            update_error = str(exc)
            update_attempt += 1
            continue

        extra = set(updates.keys()) - allowed_files
        if extra:
            update_error = f"Updates include unapproved files: {sorted(extra)}"
            update_attempt += 1
            continue
        patch_text, touched_files = _build_patch_from_updates(repo_root, updates)
        if not patch_text or not touched_files:
            update_error = "No changes found in updates."
            update_attempt += 1
            continue
        try:
            validate_touched_files(repo_root, touched_files, allowed_files)
        except PatchValidationError as exc:
            update_error = str(exc)
            update_attempt += 1
            continue
        gate = evaluate_write_phase_gate(current_phase, touched_files)
        if not gate.allowed:
            blocked_list = ", ".join(gate.blocked_files[:8])
            blocked_suffix = "..." if len(gate.blocked_files) > 8 else ""
            update_error = (
                f"{gate.reason} Blocked files: {blocked_list}{blocked_suffix}. "
                "If implementation should proceed, return a check_in phase transition request."
            )
            update_attempt += 1
            continue
        return updates, patch_text, touched_files

    raise RuntimeError(update_error or "Failed to obtain valid file updates.")
