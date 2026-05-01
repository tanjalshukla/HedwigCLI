from __future__ import annotations

"""Read-request evaluation and enforcement for `hw run`."""

import time
from pathlib import Path

import typer
from rich import print

from ..agent_client import ClaudeClient
from ..autonomy import (
    adjusted_policy_thresholds,
)
from ..config import SAConfig, autonomy_profile
from ..policy import PolicyDecision
from .helpers import (
    AutonomyHistoryContext,
    StudyContext,
    _approved_action_context,
    _apply_feedback_learning,
    _append_file_context,
    _auto_read_user_decision,
    _constraint_index,
    _policy_decision_for_file,
)
from .traces import _policy_checkin_initiators, _record_traces
from .ui import (
    _confirm_read_missing,
    _prompt_read,
    _render_auto_approve_summary,
    _render_autonomy_rationale,
    _render_file_list,
    _render_history_context,
    _render_policy_snapshot,
    _summarize_autonomy_rationale,
)
from ..schema import ReadRequest
from ..session import ClaudeSession
from ..session_feedback import SessionFeedback
from ..trust_db import PolicyHistory, TrustDB


def _record_auto_read_traces(
    *,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    task: str,
    auto_reads: list[str],
    requested: list[str],
    read_histories: dict[str, PolicyHistory],
    read_policies: dict[str, PolicyDecision],
    read_leases: dict[str, str | None],
    study_context: StudyContext | None = None,
) -> None:
    for path in auto_reads:
        auto_user_decision = _auto_read_user_decision(path, read_leases, read_policies)
        _record_traces(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            task=task,
            stage="read",
            action_type="read_request",
            files=[path],
            histories=read_histories,
            policies=read_policies,
            user_decision=auto_user_decision,
            response_time_ms=None,
            change_types={path: None},
            diff_sizes={path: None},
            blast_radius=len(requested),
            existing_leases=read_leases,
            study_context=study_context,
        )


def _process_read_request(
    *,
    request: ReadRequest,
    repo_root: Path,
    config: SAConfig,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    task: str,
    session: ClaudeSession,
    feedback: SessionFeedback,
    client: ClaudeClient | None = None,
    study_context: StudyContext | None = None,
) -> None:
    requested = request.files
    if not requested:
        print("[red]Read request contained no files.[/red]")
        raise typer.Exit(code=1)

    missing_reads = [path for path in requested if not (repo_root / path).exists()]
    if missing_reads and not _confirm_read_missing(missing_reads):
        print("[yellow]Read request denied.[/yellow]")
        raise typer.Exit(code=0)

    active_reads = trust_db.active_read_leases(repo_root_str, requested)
    read_constraints = _constraint_index(trust_db, repo_root_str, requested, access_type="read")
    read_histories: dict[str, PolicyHistory] = {}
    read_policies: dict[str, PolicyDecision] = {}
    read_leases: dict[str, str | None] = {}
    needs_prompt: list[str] = []
    auto_reads: list[str] = []
    flagged_auto_reads: list[str] = []
    denied_reads: list[str] = []
    recent_read_denials = trust_db.recent_denials(
        repo_root_str,
        run_session_id,
        stage="read",
        window_seconds=config.policy_recent_denials_window_sec,
    )
    autonomy_preferences = trust_db.autonomy_preferences(repo_root_str)
    model_checkin_total, model_checkin_rate = trust_db.model_checkin_calibration(repo_root_str)
    profile = autonomy_profile(config)

    # Evaluate policy outcome per requested path.
    for path in requested:
        history = trust_db.policy_history(repo_root_str, path, stage="read")
        read_histories[path] = history

        constraint = trust_db.strongest_constraint(repo_root_str, path, access_type="read")
        if constraint is not None:
            read_policy = constraint.policy_for("read")
            read_leases[path] = read_policy
            if read_policy == "always_deny":
                read_policies[path] = PolicyDecision(
                    action="check_in",
                    score=-1000.0,
                    reasons=("hard constraint: always_deny",),
                )
                denied_reads.append(path)
                continue
            if read_policy == "always_check_in":
                read_policies[path] = PolicyDecision(
                    action="check_in",
                    score=-500.0,
                    reasons=("hard constraint: always_check_in",),
                )
                needs_prompt.append(path)
                continue
            if read_policy == "always_allow":
                read_policies[path] = PolicyDecision(
                    action="proceed",
                    score=900.0,
                    reasons=("hard constraint: always_allow",),
                )
                auto_reads.append(path)
                continue

        lease = active_reads.get(path)
        read_leases[path] = lease.lease_type if lease else None
        if lease is not None:
            read_policies[path] = PolicyDecision(
                action="proceed",
                score=1000.0,
                reasons=("active read lease",),
            )
            auto_reads.append(path)
            continue

        if config.adaptive_policy_enabled:
            proceed_threshold, flag_threshold = adjusted_policy_thresholds(
                profile.proceed_threshold,
                profile.flag_threshold,
                autonomy_preferences,
                file_path=path,
                model_checkin_approval_rate=model_checkin_rate,
                model_checkin_total=model_checkin_total,
            )
            decision = _policy_decision_for_file(
                history=history,
                diff_size=0,
                blast_radius=len(requested),
                is_new_file=False,
                is_security_sensitive=False,
                change_pattern="read",
                recent_denials=recent_read_denials,
                files_in_action=len(requested),
                verification_failure_rate=None,
                model_confidence_avg=None,
                model_confidence_samples=0,
                proceed_threshold=proceed_threshold,
                flag_threshold=flag_threshold,
            )
        else:
            decision = PolicyDecision(
                action="check_in",
                score=0.0,
                reasons=("adaptive policy disabled",),
            )
        read_policies[path] = decision
        if decision.action == "check_in":
            needs_prompt.append(path)
        else:
            auto_reads.append(path)
            if decision.action == "proceed_flag":
                flagged_auto_reads.append(path)

    _render_policy_snapshot(
        stage="read",
        files=requested,
        histories=read_histories,
        policies=read_policies,
    )
    history_context: AutonomyHistoryContext | None = None
    rationale = None
    if not needs_prompt and not denied_reads:
        history_context, rationale = _approved_action_context(
            trust_db=trust_db,
            repo_root=repo_root_str,
            stage="read",
            task=task,
            files=requested,
            histories=read_histories,
            policies=read_policies,
            client=client,
        )
    if not needs_prompt and not denied_reads:
        _render_auto_approve_summary(
            "read",
            history_context.quantitative if history_context else None,
            history_context.qualitative if history_context else None,
            rationale or _summarize_autonomy_rationale(files=requested, policies=read_policies),
        )

    if denied_reads:
        print("[red]Read denied by hard constraints:[/red]")
        _render_file_list(denied_reads)
        trust_db.record_decision(
            repo_root_str,
            task,
            "read",
            approved=False,
            remembered=False,
            planned_files=requested,
            touched_files=requested,
        )
        _record_traces(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            task=task,
            stage="read",
            action_type="read_request",
            files=requested,
            histories=read_histories,
            policies=read_policies,
            user_decision="deny",
            response_time_ms=None,
            change_types={path: None for path in requested},
            diff_sizes={path: None for path in requested},
            blast_radius=len(requested),
            existing_leases=read_leases,
            study_context=study_context,
        )
        feedback.note_decision(False)
        raise typer.Exit(code=1)

    auto_without_lease = [path for path in auto_reads if read_leases[path] is None]
    if needs_prompt:
        prompt_started = time.time()
        approved, remembered, read_feedback = _prompt_read(needs_prompt, request.reason)
        response_time_ms = int((time.time() - prompt_started) * 1000)
        trust_db.record_decision(
            repo_root_str,
            task,
            "read",
            approved=approved,
            remembered=remembered,
            planned_files=requested,
            touched_files=requested,
        )
        if not approved:
            _record_traces(
                trust_db=trust_db,
                repo_root=repo_root_str,
                session_id=run_session_id,
                task=task,
                stage="read",
                action_type="read_request",
                files=requested,
                histories=read_histories,
                policies=read_policies,
                user_decision="deny",
                response_time_ms=response_time_ms,
                change_types={path: None for path in requested},
                diff_sizes={path: None for path in requested},
                blast_radius=len(requested),
                existing_leases=read_leases,
                user_feedback_text=read_feedback,
                check_in_initiators=_policy_checkin_initiators(requested, read_policies),
                study_context=study_context,
            )
            feedback.note_decision(
                False,
                response_time_ms=response_time_ms,
                feedback_text=read_feedback,
            )
            if read_feedback:
                _apply_feedback_learning(
                    trust_db=trust_db,
                    repo_root=repo_root_str,
                    session=session,
                    feedback_text=read_feedback,
                    client=client,
                    guidance_prefix="Denied read request guidance",
                )
            print("[yellow]Read request denied.[/yellow]")
            raise typer.Exit(code=0)

        trust_db.add_permanent_read_leases(repo_root_str, auto_without_lease, source="policy_auto")
        if remembered:
            prompt_grants = [path for path in needs_prompt if read_constraints.get(path) is None]
            trust_db.add_permanent_read_leases(repo_root_str, prompt_grants, source="user_permanent")

        _record_auto_read_traces(
            trust_db=trust_db,
            repo_root_str=repo_root_str,
            run_session_id=run_session_id,
            task=task,
            auto_reads=auto_reads,
            requested=requested,
            read_histories=read_histories,
            read_policies=read_policies,
            read_leases=read_leases,
            study_context=study_context,
        )
        _record_traces(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            task=task,
            stage="read",
            action_type="read_request",
            files=needs_prompt,
            histories=read_histories,
            policies=read_policies,
            user_decision="approve_and_remember" if remembered else "approve",
            response_time_ms=response_time_ms,
            change_types={path: None for path in needs_prompt},
            diff_sizes={path: None for path in needs_prompt},
            blast_radius=len(requested),
            existing_leases=read_leases,
            user_feedback_text=read_feedback,
            check_in_initiators=_policy_checkin_initiators(needs_prompt, read_policies),
            study_context=study_context,
        )
        feedback.note_decision(True, response_time_ms=response_time_ms)
        _apply_feedback_learning(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session=session,
            feedback_text=read_feedback,
            client=client,
        )
    else:
        trust_db.record_decision(
            repo_root_str,
            task,
            "read",
            approved=True,
            remembered=False,
            planned_files=requested,
            touched_files=requested,
        )
        if flagged_auto_reads:
            print("[yellow]Read approved. Flagged for review:[/yellow]")
            _render_file_list(flagged_auto_reads)
        trust_db.add_permanent_read_leases(repo_root_str, auto_without_lease, source="policy_auto")
        _record_auto_read_traces(
            trust_db=trust_db,
            repo_root_str=repo_root_str,
            run_session_id=run_session_id,
            task=task,
            auto_reads=auto_reads,
            requested=requested,
            read_histories=read_histories,
            read_policies=read_policies,
            read_leases=read_leases,
            study_context=study_context,
        )
        feedback.note_decision(True)

    _append_file_context(session, requested, repo_root, config.read_max_chars)
