from __future__ import annotations

"""Apply-stage policy decisions and write/verification execution for `hw run`."""

import hashlib
import os
import tempfile
import time
from pathlib import Path

import typer
from rich import print

from ..agent_client import ClaudeClient
from ..autonomy import (
    adjusted_policy_thresholds,
)
from ..config import SAConfig, autonomy_profile
from ..features import classify_change_pattern, estimate_blast_radius, is_security_sensitive
from ..ml_policy import PolicyClassifier, build_warm_start_classifier
from ..policy import PolicyDecision, within_scope_budget
from .helpers import (
    AutonomyHistoryContext,
    StudyContext,
    _approved_action_context,
    _apply_feedback_learning,
    _collect_change_metrics,
    _constraint_index,
    _normalize_new_content,
    _policy_decision_for_file,
)
from .traces import _policy_checkin_initiators, _record_traces
from .ui import (
    _prompt_approval,
    _prompt_permanent,
    _render_auto_approve_summary,
    _render_autonomy_rationale,
    _render_file_list,
    _render_history_context,
    _render_policy_snapshot,
    _summarize_autonomy_rationale,
)
from ..schema import IntentDeclaration
from ..session import ClaudeSession
from ..session_feedback import SessionFeedback
from ..trust_db import PolicyHistory, TrustDB
from ..verification import run_verification


_HIGH_RISK_CHANGE_TYPES = {"api_change", "data_model_change", "config_change", "dependency_update"}


def _update_classifier(
    *,
    classifier: PolicyClassifier,
    trust_db: TrustDB,
    repo_root_str: str,
    files: list[str],
    histories: dict[str, PolicyHistory],
    apply_change_types: dict[str, str | None],
    apply_diff_sizes: dict[str, int | None],
    apply_blast_radii: dict[str, int],
    apply_security: dict[str, bool],
    apply_new_file: dict[str, bool],
    recent_apply_denials: int,
    approved: bool,
) -> None:
    """Online-update the classifier from the developer's decision, then persist."""
    from ..policy import PolicyInput

    for path in files:
        history = histories.get(path)
        if history is None:
            continue
        raw_change = apply_change_types.get(path) or ""
        change_pattern = raw_change.split(":", 1)[-1] if ":" in raw_change else raw_change or None
        pi = PolicyInput(
            prior_approvals=history.effective_approvals,
            prior_denials=history.denials,
            avg_response_ms=history.avg_response_ms,
            avg_edit_distance=history.avg_edit_distance or 0.0,
            diff_size=apply_diff_sizes.get(path) or 0,
            blast_radius=apply_blast_radii.get(path, 1),
            is_new_file=apply_new_file.get(path, False),
            is_security_sensitive=apply_security.get(path, False),
            change_pattern=change_pattern,
            recent_denials=recent_apply_denials,
            files_in_action=len(files),
        )
        classifier.update(pi, approved)
    trust_db.save_policy_model(repo_root_str, classifier)


def _unexpected_change_types(
    declaration: IntentDeclaration,
    actual_change_types: dict[str, str | None],
) -> tuple[str, ...]:
    expected = set(declaration.expected_change_types)
    if not expected:
        return tuple()
    unexpected = sorted(
        {
            change_type.split(":", 1)[-1]
            for change_type in actual_change_types.values()
            if change_type and change_type.split(":", 1)[-1] not in expected
        }
    )
    return tuple(unexpected)


def _apply_milestone_reasons(
    *,
    declaration: IntentDeclaration,
    touched_files: list[str],
    apply_histories: dict[str, PolicyHistory],
    apply_change_types: dict[str, str | None],
    verification_failure_rates: dict[str, float | None],
    mode: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    first_write_batch = all(
        history.approvals == 0 and history.denials == 0
        for history in apply_histories.values()
    )
    if first_write_batch and mode in {"strict", "milestone"}:
        reasons.append("first write batch in this area")

    unexpected = _unexpected_change_types(declaration, apply_change_types)
    if unexpected:
        reasons.append(f"implementation deviates from approved change types: {', '.join(unexpected)}")

    if declaration.potential_deviations:
        reasons.append("plan already flagged possible deviations")

    verification_hotspots = sorted(
        path for path, rate in verification_failure_rates.items()
        if rate is not None and rate >= 0.34
    )
    if verification_hotspots and mode != "autonomous":
        preview = ", ".join(verification_hotspots[:2])
        reasons.append(f"recent verification failures in {preview}")

    high_risk = sorted(
        {
            change_type.split(":", 1)[-1]
            for change_type in apply_change_types.values()
            if change_type and change_type.split(":", 1)[-1] in _HIGH_RISK_CHANGE_TYPES
        }
    )
    if high_risk and mode == "strict":
        reasons.append(f"high-risk milestone: {', '.join(high_risk)}")

    return tuple(dict.fromkeys(reasons))


def _evaluate_apply_stage(
    *,
    repo_root: Path,
    config: SAConfig,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    task: str,
    session: ClaudeSession,
    feedback: SessionFeedback,
    updates: dict[str, str],
    touched_files: list[str],
    declaration: IntentDeclaration,
    planned_files: list[str],
    remember: bool,
    threshold: int,
    client: ClaudeClient | None = None,
    study_context: StudyContext | None = None,
) -> None:
    """Resolve apply policy + approval flow and persist decision traces."""

    active_apply = trust_db.active_leases(repo_root_str, touched_files)
    apply_constraints = _constraint_index(trust_db, repo_root_str, touched_files, access_type="write")
    change_metrics = _collect_change_metrics(repo_root, updates)
    apply_histories: dict[str, PolicyHistory] = {}
    apply_policies: dict[str, PolicyDecision] = {}
    apply_leases: dict[str, str | None] = {}
    apply_change_types: dict[str, str | None] = {}
    apply_diff_sizes: dict[str, int | None] = {}
    apply_blast_radii: dict[str, int] = {}
    apply_security: dict[str, bool] = {}
    apply_new_file: dict[str, bool] = {}
    verification_failure_rates: dict[str, float | None] = {}
    denied_apply: list[str] = []
    recent_apply_denials = trust_db.recent_denials(
        repo_root_str,
        run_session_id,
        stage="apply",
        window_seconds=config.policy_recent_denials_window_sec,
    )
    autonomy_preferences = trust_db.autonomy_preferences(repo_root_str)
    model_checkin_total, model_checkin_rate = trust_db.model_checkin_calibration(repo_root_str)
    profile = autonomy_profile(config)

    prompt_required = False
    flagged_auto_files: list[str] = []

    # Load the per-repo classifier. Normally pre-built by `hw init`; fallback
    # here only if init was skipped (e.g. in tests or manual setup).
    classifier: PolicyClassifier | None = trust_db.load_policy_model(repo_root_str)
    if classifier is None:
        classifier = build_warm_start_classifier()
        trust_db.save_policy_model(repo_root_str, classifier)

    # Score each touched file independently, then aggregate to one approval decision.
    for path in touched_files:
        history = trust_db.policy_history(repo_root_str, path, stage="apply")
        apply_histories[path] = history

        diff_size, is_new_file = change_metrics.get(path, (0, False))
        apply_diff_sizes[path] = diff_size
        file_path = repo_root / path
        try:
            old_content = file_path.read_text()
        except Exception:
            old_content = ""
        new_content = updates.get(path, "")
        change_pattern = classify_change_pattern(path, old_content, new_content)
        blast_radius = estimate_blast_radius(repo_root, path)
        security_sensitive = is_security_sensitive(path, new_content)
        apply_change_types[path] = f"{'new_file:' if is_new_file else ''}{change_pattern}"
        apply_blast_radii[path] = blast_radius
        apply_security[path] = security_sensitive
        apply_new_file[path] = is_new_file

        constraint = apply_constraints.get(path)
        if constraint is not None:
            write_policy = constraint.policy_for("write")
            apply_leases[path] = write_policy
            if write_policy == "always_deny":
                apply_policies[path] = PolicyDecision(
                    action="check_in",
                    score=-1000.0,
                    reasons=("hard constraint: always_deny",),
                )
                denied_apply.append(path)
                continue
            if write_policy == "always_check_in":
                apply_policies[path] = PolicyDecision(
                    action="check_in",
                    score=-500.0,
                    reasons=("hard constraint: always_check_in",),
                )
                prompt_required = True
                continue
            if write_policy == "always_allow":
                apply_policies[path] = PolicyDecision(
                    action="proceed",
                    score=900.0,
                    reasons=("hard constraint: always_allow",),
                )
                continue

        lease = active_apply.get(path)
        apply_leases[path] = lease.lease_type if lease else None
        if lease is not None:
            apply_policies[path] = PolicyDecision(
                action="proceed",
                score=1000.0,
                reasons=("active write lease",),
            )
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
            verification_failure_rate = trust_db.verification_failure_rate(
                repo_root_str,
                path,
                stage="apply",
            )
            verification_failure_rates[path] = verification_failure_rate
            confidence_stats = trust_db.model_confidence_stats(
                repo_root_str,
                file_path=path,
            )
            decision = _policy_decision_for_file(
                history=history,
                diff_size=diff_size,
                blast_radius=blast_radius,
                is_new_file=is_new_file,
                is_security_sensitive=security_sensitive,
                change_pattern=change_pattern,
                recent_denials=recent_apply_denials,
                files_in_action=len(touched_files),
                verification_failure_rate=verification_failure_rate,
                model_confidence_avg=confidence_stats.average,
                model_confidence_samples=confidence_stats.samples,
                proceed_threshold=proceed_threshold,
                flag_threshold=flag_threshold,
                classifier=classifier,
            )
        else:
            decision = PolicyDecision(
                action="check_in",
                score=0.0,
                reasons=("adaptive policy disabled",),
            )
        apply_policies[path] = decision
        if decision.action == "check_in":
            prompt_required = True
        elif decision.action == "proceed_flag":
            flagged_auto_files.append(path)

    milestone_reasons = _apply_milestone_reasons(
        declaration=declaration,
        touched_files=touched_files,
        apply_histories=apply_histories,
        apply_change_types=apply_change_types,
        verification_failure_rates=verification_failure_rates,
        mode=profile.mode,
    )
    if milestone_reasons:
        prompt_required = True

    _render_policy_snapshot(
        stage="apply",
        files=touched_files,
        histories=apply_histories,
        policies=apply_policies,
        force=prompt_required or bool(denied_apply) or bool(milestone_reasons),
    )
    history_context: AutonomyHistoryContext | None = None
    rationale = None
    if not prompt_required and not denied_apply and not milestone_reasons:
        history_context, rationale = _approved_action_context(
            trust_db=trust_db,
            repo_root=repo_root_str,
            stage="apply",
            task=task,
            files=touched_files,
            histories=apply_histories,
            policies=apply_policies,
            client=client,
        )
        _render_auto_approve_summary(
            "apply",
            history_context.quantitative if history_context else None,
            history_context.qualitative if history_context else None,
            rationale or _summarize_autonomy_rationale(
                files=touched_files,
                policies=apply_policies,
                milestone_reasons=milestone_reasons,
            ),
        )

    if denied_apply:
        print("[red]Patch denied by hard constraints:[/red]")
        _render_file_list(denied_apply)
        trust_db.record_decision(
            repo_root_str,
            task,
            "apply",
            approved=False,
            remembered=False,
            planned_files=planned_files,
            touched_files=touched_files,
        )
        _record_traces(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            task=task,
            stage="apply",
            action_type="write_request",
            files=touched_files,
            histories=apply_histories,
            policies=apply_policies,
            user_decision="deny",
            response_time_ms=None,
            change_types=apply_change_types,
            diff_sizes=apply_diff_sizes,
            blast_radius=len(touched_files),
            existing_leases=apply_leases,
            study_context=study_context,
        )
        feedback.note_decision(False, change_patterns=[item for item in apply_change_types.values() if item])
        raise typer.Exit(code=1)

    approved = True
    remembered = False
    response_time_ms: int | None = None

    # Split files into those needing check-in vs auto-approved so
    # the user only sees files that actually need their decision.
    if prompt_required:
        check_in_files = [
            p for p in touched_files
            if apply_policies[p].action == "check_in" and p not in denied_apply
        ]
        auto_files = [
            p for p in touched_files
            if p not in check_in_files and p not in denied_apply
        ]

        if auto_files:
            print("\nAlso included (approved automatically):")
            _render_file_list(auto_files)

        allow_remember = remember and within_scope_budget(check_in_files, config.scope_budget_files)
        prompt_started = time.time()
        approved, remembered, apply_feedback = _prompt_approval("apply", check_in_files, allow_remember)
        response_time_ms = int((time.time() - prompt_started) * 1000)
        trust_db.record_decision(
            repo_root_str,
            task,
            "apply",
            approved=approved,
            remembered=remembered,
            planned_files=planned_files,
            touched_files=touched_files,
        )
        # Record traces for auto-approved files (outcome depends on user's decision).
        if auto_files:
            _record_traces(
                trust_db=trust_db,
                repo_root=repo_root_str,
                session_id=run_session_id,
                task=task,
                stage="apply",
                action_type="write_request",
                files=auto_files,
                histories=apply_histories,
                policies=apply_policies,
                user_decision="auto_approve" if approved else "deny",
                response_time_ms=None,
                change_types=apply_change_types,
                diff_sizes=apply_diff_sizes,
                blast_radius=len(touched_files),
                existing_leases=apply_leases,
                study_context=study_context,
            )
        # Record traces for check-in files with user's actual decision.
        prompted_decision = (
            "approve_and_remember" if approved and remembered
            else ("approve" if approved else "deny")
        )
        _record_traces(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            task=task,
            stage="apply",
            action_type="write_request",
            files=check_in_files,
            histories=apply_histories,
            policies=apply_policies,
            user_decision=prompted_decision,
            response_time_ms=response_time_ms,
            change_types=apply_change_types,
            diff_sizes=apply_diff_sizes,
            blast_radius=len(touched_files),
            existing_leases=apply_leases,
            user_feedback_text=apply_feedback,
            check_in_initiators=_policy_checkin_initiators(check_in_files, apply_policies),
            study_context=study_context,
        )
        feedback.note_decision(
            approved,
            change_patterns=[item for item in apply_change_types.values() if item] if not approved else None,
            response_time_ms=response_time_ms,
            feedback_text=apply_feedback,
        )
        # Update on check-in files (explicit decision) and auto-approved files (outcome known).
        _update_classifier(
            classifier=classifier,
            trust_db=trust_db,
            repo_root_str=repo_root_str,
            files=check_in_files + (auto_files if approved else []),
            histories=apply_histories,
            apply_change_types=apply_change_types,
            apply_diff_sizes=apply_diff_sizes,
            apply_blast_radii=apply_blast_radii,
            apply_security=apply_security,
            apply_new_file=apply_new_file,
            recent_apply_denials=recent_apply_denials,
            approved=approved,
        )
        _apply_feedback_learning(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session=session,
            feedback_text=apply_feedback,
            client=client,
            guidance_prefix="Write decision guidance",
        )
        if not approved:
            print("[yellow]Patch denied.[/yellow]")
            raise typer.Exit(code=0)
        if remembered:
            remember_targets = [
                path
                for path in check_in_files
                if apply_constraints.get(path) is None
            ]
            trust_db.add_leases(
                repo_root_str,
                remember_targets,
                ttl_hours=config.lease_ttl_hours,
                source="user_remember",
            )
        # Lease offer: only for files that actually needed check-in.
        if remember and threshold > 0:
            counts = trust_db.approved_apply_counts(repo_root_str, check_in_files)
            active_for_prompt = trust_db.active_leases(repo_root_str, check_in_files)
            eligible = [
                path
                for path in check_in_files
                if counts.get(path, 0) >= threshold
                and apply_constraints.get(path) is None
                and not (path in active_for_prompt and active_for_prompt[path].expires_at is None)
            ]
            if eligible and _prompt_permanent(eligible):
                trust_db.add_permanent_leases(
                    repo_root_str,
                    eligible,
                    source="user_permanent",
                )
        return

    if all(path in active_apply for path in touched_files):
        print("[green]Apply approved.[/green]")
        user_decision = "auto_approve_lease"
    else:
        if flagged_auto_files:
            print("[yellow]Apply approved. Flagged for review:[/yellow]")
            _render_file_list(flagged_auto_files)
            user_decision = "auto_approve_flag"
        else:
            print("[green]Apply approved.[/green]")
            user_decision = "auto_approve"
    trust_db.record_decision(
        repo_root_str,
        task,
        "apply",
        approved=True,
        remembered=False,
        planned_files=planned_files,
        touched_files=touched_files,
    )
    _record_traces(
        trust_db=trust_db,
        repo_root=repo_root_str,
        session_id=run_session_id,
        task=task,
        stage="apply",
        action_type="write_request",
        files=touched_files,
        histories=apply_histories,
        policies=apply_policies,
        user_decision=user_decision,
        response_time_ms=None,
        change_types=apply_change_types,
        diff_sizes=apply_diff_sizes,
        blast_radius=len(touched_files),
        existing_leases=apply_leases,
        study_context=study_context,
    )
    feedback.note_decision(True)
    _update_classifier(
        classifier=classifier,
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        files=touched_files,
        histories=apply_histories,
        apply_change_types=apply_change_types,
        apply_diff_sizes=apply_diff_sizes,
        apply_blast_radii=apply_blast_radii,
        apply_security=apply_security,
        apply_new_file=apply_new_file,
        recent_apply_denials=recent_apply_denials,
        approved=True,
    )


def _apply_updates_and_verify(
    *,
    repo_root: Path,
    config: SAConfig,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    declaration: IntentDeclaration,
    updates: dict[str, str],
    touched_files: list[str],
    file_hashes: dict[str, str],
) -> None:
    """Write approved updates to disk and attach verification results to traces."""

    for path in touched_files:
        file_path = repo_root / path
        try:
            current = file_path.read_text()
        except Exception:
            current = ""
        current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if current_hash != file_hashes.get(path):
            print(f"[red]File changed since model response: {path}[/red]")
            raise typer.Exit(code=1)

    _write_updates_atomically(repo_root=repo_root, updates=updates, touched_files=touched_files)

    if config.verification_enabled:
        verification_result = run_verification(
            repo_root=repo_root,
            touched_files=touched_files,
            expected_behavior=declaration.task_summary,
            timeout_sec=config.verification_timeout_sec,
            command=config.verification_command,
        )
        trust_db.attach_verification_result(
            repo_root=repo_root_str,
            session_id=run_session_id,
            files=touched_files,
            verification_passed=verification_result.passed,
            verification_checks_json=verification_result.checks_json(),
            expected_behavior=verification_result.expected_behavior,
        )
        if verification_result.passed:
            print("[green]Verification passed.[/green]")
        else:
            print("[yellow]Verification reported failures.[/yellow]")
            print(
                "[dim]Autonomy rationale (verification): future autonomy will tighten in this area until verification stabilizes.[/dim]"
            )
            for check in verification_result.checks:
                if check.passed:
                    continue
                print(f"  - {check.name}: {check.output}")


def _write_updates_atomically(
    *,
    repo_root: Path,
    updates: dict[str, str],
    touched_files: list[str],
) -> None:
    temp_paths: dict[str, Path] = {}
    try:
        for path in touched_files:
            content = updates.get(path)
            if content is None:
                continue
            file_path = repo_root / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                current = file_path.read_text()
            except Exception:
                current = ""
            normalized = _normalize_new_content(current, content)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=file_path.parent,
                prefix=f".{file_path.name}.sc_tmp_",
                delete=False,
            ) as handle:
                handle.write(normalized)
                temp_paths[path] = Path(handle.name)

        for path in touched_files:
            temp_path = temp_paths.get(path)
            if temp_path is None:
                continue
            target_path = repo_root / path
            os.replace(temp_path, target_path)
            temp_paths.pop(path, None)
    except OSError as exc:
        print(f"[red]Failed to write file updates atomically: {exc}[/red]")
        raise typer.Exit(code=1)
    finally:
        for temp_path in temp_paths.values():
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
