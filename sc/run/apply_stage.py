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
    autonomy_prefs_to_preferences,
)
from ..config import SAConfig, autonomy_profile
from ..features import RiskSignals, assess_risk, change_type_label
from ..ml_policy import PolicyClassifier, build_cold_classifier
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
import json

from ..preference_inference import (
    hypothesize_from_session,
    infer_coding_mode,
    infer_task_intent,
    infer_turn_purpose,
    infer_user_persona,
    pushback_counts_from_rows,
    summarize_session,
)
from ..preferences import (
    force_action_from_preferences,
    match_confirmed_preferences,
    match_default_preferences,
    preference_from_dict,
    preference_to_dict,
)
from .apply_ui import (
    maybe_offer_permanent_lease,
    render_apply_auto_approved,
    render_apply_auto_approve_summary,
    render_apply_checkin_prompt,
    render_apply_denied,
    render_apply_policy_snapshot,
    render_hard_constraint_deny,
    render_soft_checkin_gate,
)
from .hypothesis_ui import render_hypothesis_confirmation
from .traces import _policy_checkin_initiators, _record_traces
from .ui import _render_file_list
from ..schema import IntentDeclaration
from ..session import ClaudeSession
from ..session_feedback import SessionFeedback
from ..trust_db import PolicyHistory, TrustDB
from ..verification import run_verification


_HIGH_RISK_CHANGE_TYPES = {"api_change", "data_model_change", "config_change", "dependency_update"}


def _apply_regret_corrections(
    *,
    classifier: PolicyClassifier,
    trust_db: TrustDB,
    repo_root_str: str,
    session_row_dicts: list[dict],
    recent_apply_denials: int,
) -> int:
    """Retroactively correct the classifier for regret events in this session.

    A regret is an auto-approved action that the developer later denied,
    pushed back on, or that failed verification. For each new regret event,
    we issue classifier.update(pi, approved=False) to counteract the original
    auto-approve signal. Returns the number of corrections applied.
    """
    from ..policy import PolicyInput
    from ..regret import detect_regret_events

    events = detect_regret_events(session_row_dicts)
    if not events:
        return 0

    corrections = 0
    for event in events:
        # Reconstruct a PolicyInput from the auto-approve trace that caused
        # the regret. Look up file history at the time; use what we have.
        history = trust_db.policy_history(repo_root_str, event.file_path, stage="apply")
        # We don't have the exact RiskSignals from the original trace, but
        # diff_size and blast_radius are stored.
        regret_row = next(
            (r for r in session_row_dicts if r.get("id") == event.auto_approve_trace_id),
            None,
        )
        if regret_row is None:
            continue
        pi = PolicyInput(
            prior_approvals=max(0.0, history.effective_approvals - 1),
            prior_denials=history.denials,
            avg_response_ms=history.avg_response_ms,
            avg_edit_distance=history.avg_edit_distance or 0.0,
            diff_size=int(regret_row.get("diff_size") or 0),
            blast_radius=int(regret_row.get("blast_radius") or 1),
            is_new_file=False,
            is_security_sensitive=False,
            change_pattern=str(regret_row.get("change_type") or "general_change"),
            recent_denials=recent_apply_denials,
            files_in_action=1,
        )
        classifier.update(pi, approved=False)
        corrections += 1

    if corrections:
        trust_db.save_policy_model(repo_root_str, classifier)
    return corrections


_RUBBER_STAMP_MS = 5000  # approvals faster than this get half-weight in training


def _update_classifier(
    *,
    classifier: PolicyClassifier,
    trust_db: TrustDB,
    repo_root_str: str,
    files: list[str],
    histories: dict[str, PolicyHistory],
    apply_risk: dict[str, RiskSignals],
    recent_apply_denials: int,
    approved: bool,
    response_time_ms: int | None = None,
) -> None:
    """Online-update the classifier from the developer's decision, then persist.

    Rubber-stamp discount: quick approvals (<5s) get half-weight. The SWE-chat
    analysis showed rubber-stamp approvals correlate poorly with true satisfaction.
    SGDClassifier only accepts integer labels so we approximate 0.5 weight by
    training once as approve + once as deny — net effect is no directional push.
    """
    from ..policy import PolicyInput

    is_rubber_stamp = (
        approved
        and response_time_ms is not None
        and response_time_ms < _RUBBER_STAMP_MS
    )

    for path in files:
        history = histories.get(path)
        risk = apply_risk.get(path)
        if history is None or risk is None:
            continue
        pi = PolicyInput(
            prior_approvals=history.effective_approvals,
            prior_denials=history.denials,
            avg_response_ms=history.avg_response_ms,
            avg_edit_distance=history.avg_edit_distance or 0.0,
            diff_size=risk.diff_size,
            blast_radius=risk.blast_radius,
            is_new_file=risk.is_new_file,
            is_security_sensitive=risk.is_security_sensitive,
            change_pattern=risk.change_pattern,
            recent_denials=recent_apply_denials,
            files_in_action=len(files),
        )
        if is_rubber_stamp:
            classifier.update(pi, True)
            classifier.update(pi, False)
        else:
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
    apply_risk: dict[str, RiskSignals],
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

    change_type_labels = {p: change_type_label(r) for p, r in apply_risk.items()}
    unexpected = _unexpected_change_types(declaration, change_type_labels)
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
        {risk.change_pattern for risk in apply_risk.values()
         if risk.change_pattern in _HIGH_RISK_CHANGE_TYPES}
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
    session_intensity_override: str | None = None,
) -> None:
    """Resolve apply policy + approval flow and persist decision traces."""

    active_apply = trust_db.active_leases(repo_root_str, touched_files)
    apply_constraints = _constraint_index(trust_db, repo_root_str, touched_files, access_type="write")
    change_metrics = _collect_change_metrics(repo_root, updates)
    apply_histories: dict[str, PolicyHistory] = {}
    apply_policies: dict[str, PolicyDecision] = {}
    apply_leases: dict[str, str | None] = {}
    apply_risk: dict[str, RiskSignals] = {}
    verification_failure_rates: dict[str, float | None] = {}

    def _risk_labels() -> dict[str, str | None]:
        return {p: change_type_label(r) for p, r in apply_risk.items()}

    def _risk_diff_sizes() -> dict[str, int | None]:
        return {p: r.diff_size for p, r in apply_risk.items()}
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
        classifier = build_cold_classifier()
        trust_db.save_policy_model(repo_root_str, classifier)

    # Pre-compute session state for built-in default-preference matching
    # (e.g. FAILURE_SIGNAL_CHECKIN). Signals are Hedwig-native: current
    # task intent, turn purpose, prior developer-reported failures, and
    # recent verification failures. Together they're the Hedwig equivalent
    # of the SWE-chat failure-report predictor.
    _session_trace_rows = trust_db.session_traces(repo_root_str, run_session_id)
    _session_row_dicts = [dict(r) for r in _session_trace_rows]
    _session_summary = summarize_session(_session_row_dicts)
    _session_persona = infer_user_persona(_session_summary)
    _coding_mode = infer_coding_mode(_session_summary).value
    # REPL intensity override: /intensity active|delegating pins the value
    # so the developer's explicit choice takes precedence over inference.
    _effective_intensity = session_intensity_override or _session_persona.value
    _current_task_intent = infer_task_intent(task)
    _current_turn_purpose = infer_turn_purpose(task).value
    _recent_verif_failures = sum(
        1 for row in _session_row_dicts
        if row.get("verification_passed") == 0
    )
    _matched_defaults = match_default_preferences(
        session_summary=_session_summary,
        current_task_intent=_current_task_intent,
        stage="apply",
        recent_verification_failures=_recent_verif_failures,
    )

    # Convert AutonomyPreferences coarse toggles into equivalent Preference
    # objects so both systems contribute to force_action_from_preferences.
    # These are evaluated per-file inside the loop (path-scoped preferences
    # and change_pattern predicates require per-file RiskSignals/file_path).
    _autonomy_derived_prefs = autonomy_prefs_to_preferences(autonomy_preferences)

    # Load any preferences the developer has explicitly confirmed earlier in
    # this session. Each is a full Preference (trigger + condition + action +
    # scope + lifecycle) we persisted when they said yes to a hypothesis.
    # These will be matched per-file below so their conditions (e.g.
    # min_blast_radius) evaluate against each action's RiskSignals.
    _confirmed_prefs: list = []
    for row in trust_db.confirmed_preferences_for_session(
        repo_root_str, run_session_id
    ):
        try:
            payload = json.loads(row["preference_json"])
        except Exception:
            continue
        if not payload.get("accepted"):
            continue
        pref_dict = payload.get("preference")
        if pref_dict is None:
            continue
        try:
            _confirmed_prefs.append(preference_from_dict(pref_dict))
        except Exception:
            # Skip malformed entries — defensive; schema may evolve.
            continue

    # Session position is turn_count / estimated_total. We don't know the
    # total, so estimate 20 turns (near the V2 "active" cluster center).
    # Over-estimates compress the position but still let late-session
    # preferences fire correctly since the ratio stays monotonic.
    _session_position = min(_session_summary.n_turns / 20.0, 1.0)

    _forced_action = force_action_from_preferences(_matched_defaults)

    # Regret corrections: if prior auto-approvals were later pushed back on,
    # correct the classifier before this decision so it already incorporates
    # the signal. Only fires when the classifier is active.
    if classifier is not None:
        _apply_regret_corrections(
            classifier=classifier,
            trust_db=trust_db,
            repo_root_str=repo_root_str,
            session_row_dicts=_session_row_dicts,
            recent_apply_denials=recent_apply_denials,
        )

    # Implicit-preference hypothesis: at most one per session. If the session
    # has enough trace history and a clear behavioral pattern, surface it
    # immediately so the developer can confirm or decline. Confirmed ones are
    # persisted with provenance="inferred_user_confirmed".
    if not trust_db.session_has_confirmed_hypothesis(repo_root_str, run_session_id):
        _pushback_counts = pushback_counts_from_rows(_session_row_dicts)
        from ..preferences import UserPersona as _UP
        _effective_persona = (
            _UP(_effective_intensity) if _effective_intensity in ("active", "delegating", "unknown")
            else _session_persona
        )
        _hypothesis = hypothesize_from_session(
            _session_summary,
            _pushback_counts,
            inferred_persona=_effective_persona,
            recent_verification_failures=_recent_verif_failures,
        )
        if _hypothesis is not None:
            _confirmation = render_hypothesis_confirmation(_hypothesis)
            # Persist the hypothesis outcome. On confirm we serialize the
            # full Preference (so future turns can match against it).
            # On decline we save a marker so we don't re-ask this session.
            if _confirmation.confirmed:
                _payload = {
                    "accepted": True,
                    "driver": _hypothesis.driver,
                    "preference": preference_to_dict(_hypothesis.proposed_preference),
                }
            else:
                _payload = {"accepted": False, "driver": _hypothesis.driver}
            trust_db.save_confirmed_preference(
                repo_root=repo_root_str,
                session_id=run_session_id,
                preference_json=json.dumps(_payload),
                driver=_hypothesis.driver,
            )

    # Score each touched file independently, then aggregate to one approval decision.
    for path in touched_files:
        history = trust_db.policy_history(repo_root_str, path, stage="apply")
        apply_histories[path] = history

        diff_size, is_new_file = change_metrics.get(path, (0, False))
        file_path = repo_root / path
        try:
            old_content = file_path.read_text()
        except Exception:
            old_content = ""
        new_content = updates.get(path, "")
        risk = assess_risk(
            repo_root=repo_root,
            file_path=path,
            old_content=old_content,
            new_content=new_content,
            is_new_file=is_new_file,
            diff_size=diff_size,
        )
        apply_risk[path] = risk

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
                session_intensity=_effective_intensity,
                coding_mode=_coding_mode,
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
                risk=risk,
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
        # Confirmed preferences: per-file, evaluate every user-confirmed
        # preference against this action's risk + session state. Matched ones
        # combine with the default-preference matches to determine the final
        # forced action.
        _matched_confirmed = match_confirmed_preferences(
            tuple(_confirmed_prefs),
            risk=risk,
            session_summary=_session_summary,
            current_task_intent=_current_task_intent,
            stage="apply",
            file_path=path,
            session_position=_session_position,
            session_id=run_session_id,
            current_turn_purpose=_current_turn_purpose,
            recent_verification_failures=_recent_verif_failures,
        )
        # AutonomyPreferences-derived Preferences: match per-file so that
        # path-scoped AUTO_APPLY and topic-scoped FULL_CHECKIN preferences
        # both feed force_action_from_preferences alongside the built-in
        # defaults and developer-confirmed preferences.
        _matched_autonomy = match_confirmed_preferences(
            _autonomy_derived_prefs,
            risk=risk,
            session_summary=_session_summary,
            current_task_intent=_current_task_intent,
            stage="apply",
            file_path=path,
            session_position=_session_position,
            session_id=run_session_id,
            current_turn_purpose=_current_turn_purpose,
            recent_verification_failures=_recent_verif_failures,
        )
        _all_matched = _matched_defaults + _matched_confirmed + _matched_autonomy
        _file_forced_action = force_action_from_preferences(_all_matched)

        # Preferences can tighten or loosen the scorer's action.
        # Asymmetry: tightening always applies; loosening (auto_apply) only
        # applies if the scorer didn't already decide to check_in.
        if _file_forced_action is not None:
            if _file_forced_action.value == "full_checkin" and decision.action != "check_in":
                from_confirmed = any(
                    p.lifecycle.provenance == "inferred_user_confirmed"
                    for p in _matched_confirmed
                    if p.action.value == "full_checkin"
                )
                reason = (
                    "confirmed preference forced check-in"
                    if from_confirmed
                    else "failure-signal trigger: debug intent + prior failure this session"
                )
                decision = PolicyDecision(
                    action="check_in",
                    score=decision.score,
                    reasons=decision.reasons + (reason,),
                )
            elif _file_forced_action.value == "soft_checkin" and decision.action not in ("check_in",):
                decision = PolicyDecision(
                    action="proceed_flag",
                    score=decision.score,
                    reasons=decision.reasons + ("soft-checkin trigger matched",),
                )
            elif _file_forced_action.value == "auto_apply" and decision.action == "check_in":
                # AUTO_APPLY from AutonomyPreferences (prefer_fewer_checkins).
                # Only loosens when the scorer decided to check_in — never bypasses
                # hard constraints (those set score to -1000 or -500 and skip this path).
                decision = PolicyDecision(
                    action="proceed",
                    score=decision.score,
                    reasons=decision.reasons + ("autonomy preference: proceed autonomously",),
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
        apply_risk=apply_risk,
        verification_failure_rates=verification_failure_rates,
        mode=profile.mode,
    )
    if milestone_reasons:
        prompt_required = True

    render_apply_policy_snapshot(
        touched_files=touched_files,
        histories=apply_histories,
        policies=apply_policies,
        prompt_required=prompt_required,
        denied_apply=denied_apply,
        milestone_reasons=milestone_reasons,
    )
    history_context: AutonomyHistoryContext | None = None
    if not prompt_required and not denied_apply and not milestone_reasons:
        history_context, _ = _approved_action_context(
            trust_db=trust_db,
            repo_root=repo_root_str,
            stage="apply",
            task=task,
            files=touched_files,
            histories=apply_histories,
            policies=apply_policies,
            client=client,
        )
        render_apply_auto_approve_summary(
            touched_files=touched_files,
            policies=apply_policies,
            quantitative=history_context.quantitative if history_context else None,
            qualitative=history_context.qualitative if history_context else None,
            milestone_reasons=milestone_reasons,
        )

    if denied_apply:
        render_hard_constraint_deny(denied_apply)
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
            change_types=_risk_labels(),
            diff_sizes=_risk_diff_sizes(),
            blast_radius=len(touched_files),
            existing_leases=apply_leases,
            study_context=study_context,
        )
        feedback.note_decision(False, change_patterns=[change_type_label(r) for r in apply_risk.values()])
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

        approved, remembered, apply_feedback, response_time_ms = render_apply_checkin_prompt(
            repo_root=repo_root,
            updates=updates,
            check_in_files=check_in_files,
            auto_files=auto_files,
            apply_policies=apply_policies,
            apply_risk=apply_risk,
            session_row_dicts=_session_row_dicts,
            verification_failure_rates=verification_failure_rates,
            remember=remember,
            scope_budget_files=config.scope_budget_files,
        )
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
                change_types=_risk_labels(),
                diff_sizes=_risk_diff_sizes(),
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
            change_types=_risk_labels(),
            diff_sizes=_risk_diff_sizes(),
            blast_radius=len(touched_files),
            existing_leases=apply_leases,
            user_feedback_text=apply_feedback,
            check_in_initiators=_policy_checkin_initiators(check_in_files, apply_policies),
            study_context=study_context,
        )
        feedback.note_decision(
            approved,
            change_patterns=[change_type_label(r) for r in apply_risk.values()] if not approved else None,
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
            apply_risk=apply_risk,
            recent_apply_denials=recent_apply_denials,
            approved=approved,
            response_time_ms=response_time_ms,
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
            render_apply_denied()
            raise typer.Exit(code=0)
        if remembered:
            trust_db.add_leases(
                repo_root_str,
                [p for p in check_in_files if apply_constraints.get(p) is None],
                ttl_hours=config.lease_ttl_hours,
                source="user_remember",
            )
        maybe_offer_permanent_lease(
            remember=remember,
            threshold=threshold,
            check_in_files=check_in_files,
            apply_constraints=apply_constraints,
            trust_db=trust_db,
            repo_root_str=repo_root_str,
            config=config,
        )
        return

    # If a default Preference triggered a soft check-in, render the
    # non-blocking panel and give the developer a window to intervene.
    if _forced_action is not None and _forced_action.value == "soft_checkin":
        outcome = render_soft_checkin_gate(
            touched_files=touched_files,
            apply_policies=apply_policies,
        )
        if outcome.intervened:
            from .ui import _prompt_approval as _full_prompt
            approved, remembered, apply_feedback = _full_prompt(
                "apply", touched_files, remember, diff_already_shown=False
            )
            if not approved:
                render_apply_denied(intervention=True)
                raise typer.Exit(code=0)

    user_decision = render_apply_auto_approved(
        all_leased=all(path in active_apply for path in touched_files),
        flagged_auto_files=flagged_auto_files,
        policies=apply_policies,
        touched_files=touched_files,
    )
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
        change_types=_risk_labels(),
        diff_sizes=_risk_diff_sizes(),
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
        apply_risk=apply_risk,
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
        from ..run.theme import PALETTE as _PAL_V
        if verification_result.passed:
            print(f"[{_PAL_V['approve_bold']}]✓ verification passed[/{_PAL_V['approve_bold']}]")
        else:
            print(f"[{_PAL_V['deny_bold']}]✗ verification failed[/{_PAL_V['deny_bold']}]")
            print(
                f"[{_PAL_V['meta_italic']}]future oversight will tighten in this area until this stabilizes[/{_PAL_V['meta_italic']}]"
            )
            for check in verification_result.checks:
                if check.passed:
                    continue
                print(f"  [{_PAL_V['deny']}]·[/{_PAL_V['deny']}] {check.name}: {check.output}")


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
