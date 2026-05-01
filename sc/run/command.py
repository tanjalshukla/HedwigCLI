from __future__ import annotations

"""Main `hw run` orchestration entrypoint."""

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import typer
from rich import print
from rich.syntax import Syntax

from ..agent_client import ClaudeClient
from ..cli_shared import read_file_context as _read_file_context, resolve_config as _resolve_config
from ..config import SAConfig, autonomy_profile, config_dir
from ..plan_gate import decide_plan_checkpoint
from ..policy import PolicyDecision
from ..prompt_builder import build_run_system_prompt
from ..repo import RepoError, get_repo_root
from ..schema import CheckInMessage, IntentDeclaration, ReadRequest, WorkflowPhase
from ..session import ClaudeSession
from ..session_feedback import SessionFeedback
from ..trust_db import PolicyHistory, TrustDB
from .apply_stage import _apply_updates_and_verify, _evaluate_apply_stage
from .model import (
    _apply_phase_transition_with_display,
    _generate_updates_with_repair,
    _handle_model_checkin,
    _infer_phase_from_checkin,
    _refresh_session_context,
)
from .reporting import _finalize_run
from .read_stage import _process_read_request
from .traces import _record_traces
from .ui import (
    _confirm_create_files,
    _model_status,
    _prompt_plan_checkpoint,
    _render_intent_summary,
    _show_system_prompt,
)
from .helpers import (
    _apply_feedback_learning,
    _capture_logic_notes,
    SpecContext,
    StudyContext,
    _load_spec_context,
    _plan_hash,
)


@dataclass
class _IntentResolution:
    declaration: IntentDeclaration
    current_phase: WorkflowPhase
    intent_rendered_during_checkpoint: bool


def _resolve_intent_declaration(
    *,
    client: ClaudeClient,
    session: ClaudeSession,
    task: str,
    config: SAConfig,
    trust_db: TrustDB,
    repo_root: Path,
    repo_root_str: str,
    run_session_id: str,
    current_phase: WorkflowPhase,
    show_system_prompt: bool,
    feedback: SessionFeedback,
    study_context: StudyContext,
    spec_context: SpecContext | None,
) -> _IntentResolution:
    declaration: IntentDeclaration | None = None
    plan_revision_round = 0
    intent_rendered_during_checkpoint = False
    force_implementation_after_checkpoint = False
    profile = autonomy_profile(config)

    while declaration is None:
        try:
            session.system_prompt = build_run_system_prompt(
                trust_db=trust_db,
                repo_root=repo_root_str,
                workflow_phase=current_phase,
                autonomy_mode=profile.mode,
                task_text=task,
                spec_digest=spec_context.digest if spec_context else None,
            )
            _refresh_session_context(session, feedback)
            with _model_status("intent"):
                response = client.declare_intent(
                    session,
                    task=task,
                    max_tokens=config.max_tokens,
                    temperature=config.temperature,
                )
        except Exception as exc:
            print("[red]Failed to obtain valid intent declaration.[/red]")
            print(str(exc))
            raise typer.Exit(code=1)

        if isinstance(response, CheckInMessage):
            approved, _, _ = _handle_model_checkin(
                check_in=response,
                stage="planning",
                task=task,
                session_id=run_session_id,
                trust_db=trust_db,
                repo_root_str=repo_root_str,
                session=session,
                feedback=feedback,
                client=client,
                study_context=study_context,
            )
            if not approved:
                print("[yellow]Task denied during model check-in.[/yellow]")
                raise typer.Exit(code=0)
            next_phase = _infer_phase_from_checkin(response, current_phase)
            current_phase = _apply_phase_transition_with_display(
                session=session,
                trust_db=trust_db,
                repo_root=repo_root_str,
                current_phase=current_phase,
                next_phase=next_phase,
                show_system_prompt=show_system_prompt,
                feedback=feedback,
                autonomy_mode=study_context.autonomy_mode or "balanced",
                task_text=task,
                spec_digest=spec_context.digest if spec_context else None,
            )
            continue

        if isinstance(response, ReadRequest):
            _process_read_request(
                request=response,
                repo_root=repo_root,
                config=config,
                trust_db=trust_db,
                repo_root_str=repo_root_str,
                run_session_id=run_session_id,
                task=task,
                session=session,
                feedback=feedback,
                client=client,
                study_context=study_context,
            )
            continue

        candidate = response
        autonomy_preferences = trust_db.autonomy_preferences(repo_root_str)
        checkpoint = decide_plan_checkpoint(
            trust_db=trust_db,
            repo_root=repo_root_str,
            declaration=candidate,
            strict=profile.strict_plan_gate,
            max_auto_files=profile.plan_checkpoint_max_files,
            autonomy_preferences=autonomy_preferences,
            repo_root_path=repo_root,
            spec_required=spec_context is not None,
        )
        if checkpoint.required:
            intent_rendered_during_checkpoint = True
            prompt_started = time.time()
            plan_decision, plan_feedback = _prompt_plan_checkpoint(candidate, checkpoint.reasons)
            response_time_ms = int((time.time() - prompt_started) * 1000)
            plan_revision_round += 1
            trust_db.record_plan_revision(
                repo_root=repo_root_str,
                session_id=run_session_id,
                task=task,
                revision_round=plan_revision_round,
                plan_hash=_plan_hash(candidate),
                intent_json=json.dumps(
                    candidate.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                reasons=checkpoint.reasons,
                developer_feedback=plan_feedback,
                approved=plan_decision == "approve",
                participant_id=study_context.participant_id,
                study_run_id=study_context.study_run_id,
                study_task_id=study_context.study_task_id,
                autonomy_mode=study_context.autonomy_mode,
            )
            trust_db.record_trace(
                repo_root=repo_root_str,
                session_id=run_session_id,
                task=task,
                stage="planning",
                action_type="plan_revision",
                file_path="__plan__",
                change_type="plan_checkpoint",
                diff_size=None,
                blast_radius=len(candidate.planned_files),
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=0.0,
                user_decision=(
                    "approve"
                    if plan_decision == "approve"
                    else ("revise" if plan_decision == "revise" else "deny")
                ),
                response_time_ms=response_time_ms,
                edit_distance=None,
                user_feedback_text=plan_feedback,
                check_in_initiator="policy",
                participant_id=study_context.participant_id,
                study_run_id=study_context.study_run_id,
                study_task_id=study_context.study_task_id,
                autonomy_mode=study_context.autonomy_mode,
            )
            feedback.note_decision(
                plan_decision == "approve",
                change_patterns=["plan_checkpoint"] if plan_decision != "approve" else None,
                response_time_ms=response_time_ms,
                feedback_text=plan_feedback,
            )
            _apply_feedback_learning(
                trust_db=trust_db,
                repo_root=repo_root_str,
                session=session,
                feedback_text=plan_feedback,
                client=client,
                guidance_prefix="Plan guidance",
            )

            if plan_decision == "deny":
                print("[yellow]Task denied at plan checkpoint.[/yellow]")
                raise typer.Exit(code=0)
            if plan_decision == "revise":
                if plan_revision_round > max(config.max_plan_revisions, 0):
                    print("[red]Maximum plan revisions reached for this run.[/red]")
                    raise typer.Exit(code=1)
                revision_request = (
                    "Developer requested plan revision.\n"
                    f"Feedback: {plan_feedback or 'Reduce risk and tighten scope.'}\n"
                    "Return revised intent JSON only."
                )
                session.add_user(revision_request)
                continue
            if plan_decision == "approve" and any(
                not path.endswith(".md") for path in candidate.planned_files
            ):
                force_implementation_after_checkpoint = True

        declaration = candidate

    declared_phase: WorkflowPhase = declaration.workflow_phase or "implementation"
    if force_implementation_after_checkpoint and declared_phase == "planning":
        declared_phase = "implementation"
    current_phase = _apply_phase_transition_with_display(
        session=session,
        trust_db=trust_db,
        repo_root=repo_root_str,
        current_phase=current_phase,
        next_phase=declared_phase,
        show_system_prompt=show_system_prompt,
        feedback=feedback,
        autonomy_mode=study_context.autonomy_mode or "balanced",
        task_text=task,
        spec_digest=spec_context.digest if spec_context else None,
    )
    return _IntentResolution(
        declaration=declaration,
        current_phase=current_phase,
        intent_rendered_during_checkpoint=intent_rendered_during_checkpoint,
    )


def _record_declare_stage(
    *,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    task: str,
    declaration: IntentDeclaration,
    study_context: StudyContext,
) -> None:
    planned_files = declaration.planned_files
    trust_db.record_decision(
        repo_root_str,
        task,
        "declare",
        approved=True,
        remembered=False,
        planned_files=planned_files,
    )
    declare_histories: dict[str, PolicyHistory] = {}
    declare_policies: dict[str, PolicyDecision] = {}
    declare_change_types = {path: None for path in planned_files}
    declare_diff_sizes = {path: None for path in planned_files}
    declare_leases = {path: None for path in planned_files}
    for path in planned_files:
        history = trust_db.policy_history(repo_root_str, path, stage="declare")
        declare_histories[path] = history
        declare_policies[path] = PolicyDecision(
            action="proceed",
            score=0.0,
            reasons=("intent accepted",),
        )
    _record_traces(
        trust_db=trust_db,
        repo_root=repo_root_str,
        session_id=run_session_id,
        task=task,
        stage="declare",
        action_type="intent_declaration",
        files=planned_files,
        histories=declare_histories,
        policies=declare_policies,
        user_decision="auto_approve_intent",
        response_time_ms=None,
        change_types=declare_change_types,
        diff_sizes=declare_diff_sizes,
        blast_radius=len(planned_files),
        existing_leases=declare_leases,
        study_context=study_context,
    )


def run(
    task: str = typer.Argument(..., help="Task for the agent."),
    mode: str = typer.Option("pair", "--mode", help="Execution mode (pair or async)."),
    model_id: str = typer.Option(None, "--model-id", help="Bedrock inference profile ID/ARN."),
    region: str = typer.Option(None, "--region", help="AWS region for Bedrock."),
    remember: bool = typer.Option(True, "--remember/--no-remember", help="Allow remember leases."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff but do not apply patch."),
    show_intent: bool = typer.Option(False, "--show-intent", help="Display intent summary and plan."),
    show_system_prompt: bool = typer.Option(
        False,
        "--show-system-prompt",
        help="Display the dynamic system prompt at session start and phase transitions.",
    ),
    spec: str | None = typer.Option(
        None,
        "--spec",
        help="Optional spec artifact to align the plan and implementation against.",
    ),
    permanent_threshold: int | None = typer.Option(
        None,
        "--permanent-threshold",
        help="Approvals required before offering permanent permission.",
    ),
    participant_id: str | None = typer.Option(
        None,
        "--participant-id",
        envvar="SC_PARTICIPANT_ID",
        help="Optional study participant identifier.",
    ),
    study_run_id: str | None = typer.Option(
        None,
        "--study-run-id",
        envvar="SC_STUDY_RUN_ID",
        help="Optional study run identifier.",
    ),
    task_id: str | None = typer.Option(
        None,
        "--task-id",
        envvar="SC_TASK_ID",
        help="Optional study task identifier.",
    ),
):
    """Run the agent with intent gating and patch approval."""

    if mode != "pair":
        print("[red]Only --mode pair is currently supported.[/red]")
        raise typer.Exit(code=1)

    try:
        repo_root = get_repo_root()
    except RepoError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    try:
        config = _resolve_config(repo_root, model_id, region)
    except typer.BadParameter as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    trust_db = TrustDB(config_dir(repo_root) / "trust.db")
    repo_root_str = str(repo_root)
    profile = autonomy_profile(config)
    study_context = StudyContext(
        participant_id=participant_id,
        study_run_id=study_run_id,
        study_task_id=task_id,
        autonomy_mode=profile.mode,
    )
    print(f"[bold]Session mode[/bold] {profile.mode}")
    run_session_id = uuid4().hex
    threshold = permanent_threshold if permanent_threshold is not None else config.permanent_approval_threshold
    client = ClaudeClient(model_id=config.model_id, region=config.aws_region)
    current_phase: WorkflowPhase = "planning"
    feedback = SessionFeedback(current_phase=current_phase)
    try:
        spec_context = _load_spec_context(repo_root, spec, config.read_max_chars)
    except FileNotFoundError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
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
        _show_system_prompt(current_phase, session.system_prompt)

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

    # Stage 2: generate candidate file updates and render the resulting patch.
    file_context = _read_file_context(repo_root, planned_files, config.read_max_chars)
    file_hashes = {}
    for path in planned_files:
        try:
            current = (repo_root / path).read_text()
        except Exception:
            current = ""
        file_hashes[path] = hashlib.sha256(current.encode("utf-8")).hexdigest()
    allowed_files = set(planned_files)
    try:
        updates, patch_text, touched_files = _generate_updates_with_repair(
            client=client,
            session=session,
            declaration=declaration,
            file_context=file_context,
            allowed_files=allowed_files,
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
    except RuntimeError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    print("\n[bold]Proposed patch[/bold]")
    print(Syntax(patch_text, "diff", theme="ansi_dark", word_wrap=False))

    new_files = [path for path in touched_files if not (repo_root / path).exists()]
    if new_files and not _confirm_create_files(new_files):
        print("[yellow]Patch denied.[/yellow]")
        raise typer.Exit(code=0)

    # Stage 3: evaluate write policy and collect apply traces.
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
    )

    if dry_run:
        print("[yellow]Dry run enabled: patch not applied.[/yellow]")
        _finalize_run(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
        )
        return

    # Stage 4: apply approved writes and run optional verification.
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

    _capture_logic_notes(
        trust_db=trust_db,
        repo_root=repo_root_str,
        session_id=run_session_id,
        task=task,
        declaration=declaration,
        touched_files=touched_files,
        patch_text=patch_text,
        spec_context=spec_context,
        client=client,
    )

    print("[green]Patch applied successfully.[/green]")
    _finalize_run(
        trust_db=trust_db,
        repo_root=repo_root_str,
        session_id=run_session_id,
    )
