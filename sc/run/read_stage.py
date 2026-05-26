from __future__ import annotations

"""Read-request evaluation and enforcement for `hw run`.

Mirrors the apply-stage cascade in ``apply_stage.py``: hard constraints →
leases → PolicyScorer → preference override. The shared steps are factored
into ``helpers._resolve_pre_scorer`` (constraints + leases) and
``helpers._policy_decision_for_file`` (scorer); both stages call the same
helpers in the same order. What differs by stage and is intentional:

* **Thresholds** — reads use a more permissive ``proceed_threshold`` since
  reading a file cannot break anything (see line ~180).
* **Lease tables** — ``read_leases`` is separate from ``leases``; the
  former is a read-only grant, the latter is a write grant.
* **No regret corrections, no hypothesis pipeline, no classifier updates** —
  those live only in the apply stage. Read decisions feed traces but
  don't drive learning beyond threshold history.
* **UI surface** — read prompts are simpler (``a``/``r``/``d``) and never
  touch the apply check-in flow.

Unifying the two cascades into one parameterized module is parked
post-conference (see BRAINSTORM.md): the shared work already lives in
``helpers``, and the remaining differences are all intentional.
"""

import time
from pathlib import Path

import typer
from rich import print

from ..agent_client import ClaudeClient
from ..autonomy import (
    adjusted_policy_thresholds,
)
from ..config import SAConfig, autonomy_profile
from ..features import RiskSignals
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
    _resolve_pre_scorer,
    infer_session_intensity,
)
from .traces import _policy_checkin_initiators, _record_traces
from .ui import (
    _confirm_read_missing,
    _prompt_read,
    _render_auto_approve_summary,
    _render_file_list,
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
        from ..run.theme import PALETTE as _PAL_RE
        print(f"[{_PAL_RE['deny_bold']}]✗ read request had no files[/{_PAL_RE['deny_bold']}]")
        raise typer.Exit(code=1)

    missing_reads = [path for path in requested if not (repo_root / path).exists()]
    if missing_reads:
        # The agent asked to read a path that doesn't exist (typically a
        # hallucinated filename). Don't bother the developer — bounce the
        # mistake back to the agent with the file tree so it self-corrects.
        from ..prompt_builder import _repo_file_tree
        tree = _repo_file_tree(repo_root_str, max_files=80)
        session.add_user(
            "Your last read request listed paths that do not exist in this "
            "repository: "
            + ", ".join(missing_reads)
            + ".\n\nReal repository file tree (use these exact paths — do not "
            "invent paths):\n"
            + tree
            + "\n\nRetry the read request using only paths from the tree above."
        )
        return

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

    # Session intensity — consumed by adjusted_policy_thresholds so that active
    # sessions tighten oversight on reads too (not only writes). Shared with
    # the apply stage via helpers.infer_session_intensity to keep the signal
    # in lockstep across the two cascades.
    _session_persona, _coding_mode, _session_rows = infer_session_intensity(
        trust_db, repo_root_str, run_session_id
    )

    # Files the developer explicitly remembered (r / approve_and_remember) for
    # reading this session. Plain `a` approvals don't auto-carry forward — the
    # developer had a reason to review and shouldn't have that choice removed.
    # `r` is the explicit signal: "I trust this file for the rest of the session."
    _session_approved_reads: set[str] = {
        r["file_path"]
        for r in _session_rows
        if r.get("stage") == "read"
        and r.get("user_decision") == "approve_and_remember"
        and r.get("file_path") != "__session__"
    }

    # Evaluate policy outcome per requested path.
    for path in requested:
        history = trust_db.policy_history(repo_root_str, path, stage="read")
        read_histories[path] = history

        constraint = trust_db.strongest_constraint(repo_root_str, path, access_type="read")
        lease = active_reads.get(path)
        pre = _resolve_pre_scorer(constraint=constraint, lease=lease, access_type="read")
        if pre is not None:
            decision, lease_label, outcome = pre
            read_leases[path] = lease_label
            read_policies[path] = decision
            if outcome == "deny":
                denied_reads.append(path)
            elif outcome == "check_in":
                needs_prompt.append(path)
            elif outcome in ("allow", "lease"):
                auto_reads.append(path)
            continue
        read_leases[path] = None

        # Already approved for reading this session — don't ask again.
        if path in _session_approved_reads:
            read_policies[path] = PolicyDecision(
                action="proceed",
                score=900.0,
                reasons=("approved for reading earlier this session",),
            )
            auto_reads.append(path)
            continue

        if config.adaptive_policy_enabled:
            # Reads use a more permissive threshold than writes — reading a file
            # cannot break anything. The developer just named the task; asking
            # permission to read every mentioned file is pure friction.
            # Use a lower proceed_threshold so most reads auto-approve.
            _read_proceed = min(profile.proceed_threshold, 0.5)
            _read_flag = min(profile.flag_threshold, 0.1)
            proceed_threshold, flag_threshold = adjusted_policy_thresholds(
                _read_proceed,
                _read_flag,
                autonomy_preferences,
                file_path=path,
                model_checkin_approval_rate=model_checkin_rate,
                model_checkin_total=model_checkin_total,
                session_intensity=_session_persona,
                coding_mode=_coding_mode,
            )
            read_risk = RiskSignals(
                change_pattern="read",
                blast_radius=1,  # reads don't have blast radius in the write sense
                is_security_sensitive=False,
                is_new_file=False,
                diff_size=0,
            )
            decision = _policy_decision_for_file(
                history=history,
                risk=read_risk,
                recent_denials=recent_read_denials,
                files_in_action=1,  # multi-file reads are not multi-file writes
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
        from ..run.theme import PALETTE as _PAL_R
        print(f"[{_PAL_R['deny_bold']}]✗ read denied by hard constraint[/{_PAL_R['deny_bold']}]")
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
        approved, remember_paths, read_feedback = _prompt_read(needs_prompt, request.reason)
        response_time_ms = int((time.time() - prompt_started) * 1000)
        remembered = bool(remember_paths)
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
            from ..run.theme import PALETTE as _PAL_RD
            print(f"[{_PAL_RD['attention']}]✗ read request denied[/{_PAL_RD['attention']}]")
            raise typer.Exit(code=0)

        trust_db.add_permanent_read_leases(repo_root_str, auto_without_lease, source="policy_auto")
        if remember_paths:
            # Per-file remember: only paths the developer explicitly toggled
            # become permanent leases (and only if not already constrained).
            prompt_grants = [
                path for path in remember_paths
                if read_constraints.get(path) is None
            ]
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
            from ..run.theme import PALETTE as _PAL_RF
            print(f"[{_PAL_RF['attention']}]✓ read approved · flagged for review[/{_PAL_RF['attention']}]")
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
