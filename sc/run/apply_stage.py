from __future__ import annotations

"""Apply-stage cascade: score each touched file, prompt or auto-approve, write atomically.

Extends the shared read/apply cascade in helpers.py with apply-only steps:
regret corrections, hypothesis pipeline, classifier online updates, and
preference override. Intentional asymmetries vs read_stage are documented
in SPEC.md §Deliberate non-goals.
"""

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import typer
from rich import print

from ..agent_client import ClaudeClient
from ..autonomy import (
    adjusted_policy_thresholds,
    autonomy_prefs_to_preferences,
)
from ..config import SAConfig, autonomy_profile
from ..features import RiskSignals, assess_risk, change_type_label, parse_change_type_label
from ..model_risk import (
    assess_risk_via_model,
    ask_model_to_vote,
    is_borderline,
    should_review,
    _BORDERLINE_MAX_SHIFT,
)
from ..ml_policy import PolicyClassifier, build_cold_classifier
from ..policy import PolicyDecision, _bucket
from .helpers import (
    AutonomyHistoryContext,
    StudyContext,
    _approved_action_context,
    _apply_feedback_learning,
    _collect_change_metrics,
    _constraint_index,
    _resolve_pre_scorer,
    _normalize_new_content,
    _policy_decision_for_file,
)
from ..hypothesis_bank import (
    get_ready_hypothesis,
    mark_candidate_surfaced,
    seed_candidates_from_session,
    update_evidence,
    maybe_generate_llm_hypotheses,
)
from ..preference_inference import (
    SessionSummary,
    infer_coding_mode,
    infer_task_intent,
    infer_turn_purpose,
    infer_user_persona,
    pushback_counts_from_rows,
    summarize_session,
)
from ..preferences import (
    Preference,
    PreferenceAction,
    TaskIntent,
    UserPersona,
    force_action_from_preferences,
    match_default_preferences,
    preference_from_dict,
    preference_to_dict,
)
from ..store.types import DecisionTraceRow
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
from .preference_coordinator import PreferenceCoordinator
from .revise_state import stash as _stash_revise
from .theme import PALETTE as _THEME
from .traces import _policy_checkin_initiators, _record_traces
from .ui import _prompt_approval
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
    """Replay regret events as corrective classifier signals (count_sample=False).

    Each regret fires exactly once, tracked in classifier._corrected_regret_ids.
    Returns the number of corrections applied.
    """
    from ..policy import PolicyInput
    from ..regret import detect_regret_events

    events = detect_regret_events(session_row_dicts)
    if not events:
        return 0

    corrections = 0
    for event in events:
        regret_key = event.auto_approve_trace_id
        if regret_key in classifier._corrected_regret_ids:
            continue
        regret_row = next(
            (r for r in session_row_dicts if r.get("id") == event.auto_approve_trace_id),
            None,
        )
        if regret_row is None:
            continue
        history = trust_db.policy_history(repo_root_str, event.file_path, stage="apply")
        regret_is_new_file, change_pattern = parse_change_type_label(regret_row.get("change_type"))
        raw_sec = regret_row.get("is_security_sensitive")
        # Counteract the original auto-approve by the same weight it added to
        # effective_approvals: a rubber-stamp (<5s) contributed only 0.5, so
        # undoing a full 1.0 would over-subtract. Mirror trace_store's split.
        approve_weight = 0.5 if regret_row.get("rubber_stamp") else 1.0
        pi = PolicyInput(
            prior_approvals=max(0.0, history.effective_approvals - approve_weight),
            prior_denials=history.denials,
            avg_response_ms=history.avg_response_ms,
            avg_edit_distance=history.avg_edit_distance or 0.0,
            diff_size=int(regret_row.get("diff_size") or 0),
            blast_radius=int(regret_row.get("blast_radius") or 1),
            is_new_file=regret_is_new_file,
            is_security_sensitive=bool(raw_sec) if raw_sec is not None else False,
            change_pattern=change_pattern,
            recent_denials=recent_apply_denials,
            files_in_action=1,
        )
        classifier.update(pi, approved=False, count_sample=False)
        classifier._corrected_regret_ids.add(regret_key)
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
    """Update the classifier for the developer's decision.

    Quick approvals (<5s) get half-weight: one approve + one deny cancels out.
    This mirrors the rubber-stamp discount in policy_history.effective_approvals.
    """
    from ..policy import PolicyInput

    is_rubber_stamp = approved and response_time_ms is not None and response_time_ms < _RUBBER_STAMP_MS
    for path in files:
        history = histories.get(path)
        risk = apply_risk.get(path)
        if history is None or risk is None:
            continue
        pi = PolicyInput.from_signals(
            history, risk, recent_denials=recent_apply_denials, files_in_action=len(files),
        )
        if is_rubber_stamp:
            classifier.update(pi, True)
            classifier.update(pi, False, count_sample=False)
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


@dataclass
class _SessionContext:
    """Pre-computed session state threaded through the apply-stage pipeline.

    Extracted from _evaluate_apply_stage to make that function testable in
    isolation and to keep the session-inference logic auditable as a unit.
    All fields are read-only after construction.
    """

    session_row_dicts: list[DecisionTraceRow]
    apply_row_dicts: list[DecisionTraceRow]
    session_summary: SessionSummary
    session_persona: UserPersona
    effective_intensity: str               # "active" | "delegating" | "unknown" (possibly pinned)
    effective_persona: UserPersona
    coding_mode: str
    current_task_intent: TaskIntent
    current_turn_purpose: str
    recent_verif_failures: int
    matched_defaults: tuple[Preference, ...]
    autonomy_derived_prefs: tuple[Preference, ...]
    confirmed_prefs: list[Preference]
    session_position: float
    forced_action: PreferenceAction | None
    pushback_counts: dict[str, int]


def _load_confirmed_preferences(
    trust_db: TrustDB, repo_root_str: str, run_session_id: str
) -> list[Preference]:
    """Decode all confirmed Preference rows for this repo. Skips rows that
    didn't accept the hypothesis or whose payload no longer round-trips.

    Uses confirmed_preferences_for_repo (not session-scoped) so that
    preferences seeded under session_id='seed_demo' and preferences confirmed
    in prior live sessions both fire in the apply cascade.
    """
    confirmed: list[Preference] = []
    for row in trust_db.confirmed_preferences_for_repo(repo_root_str):
        try:
            payload = json.loads(row["preference_json"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if not payload.get("accepted"):
            continue
        pref_dict = payload.get("preference")
        if pref_dict is None:
            continue
        try:
            confirmed.append(preference_from_dict(pref_dict))
        except (TypeError, ValueError, KeyError):
            continue
    return confirmed


def _build_session_context(
    *,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    task: str,
    autonomy_preferences,
    session_intensity_override: str | None,
) -> _SessionContext:
    """Build all session-level signals in one place.

    Isolating this from the per-file scoring loop makes both halves testable
    independently and keeps the inference logic easy to audit.

    n_turns note: we summarize only apply-stage traces so that read-stage
    traces don't inflate persona/hypothesis thresholds. A session with 10
    reads but 0 writes should not trigger ACTIVE persona or hypothesis seeding.
    """
    from ..preferences import UserPersona as _UP

    all_trace_rows = trust_db.session_traces(repo_root_str, run_session_id)
    # sqlite3.Row exposes the same string-keyed access as our TypedDict, and
    # column names from session_traces() are a subset of DecisionTraceRow.
    all_row_dicts: list[DecisionTraceRow] = [
        cast(DecisionTraceRow, dict(r)) for r in all_trace_rows
    ]

    # Filter to apply-stage rows for behavioral signal inference.
    # Read-stage traces don't carry approval/denial signals meaningful for
    # persona inference or hypothesis thresholds.
    apply_row_dicts = [r for r in all_row_dicts if r.get("stage") == "apply"]
    session_summary = summarize_session(apply_row_dicts)

    session_persona = infer_user_persona(session_summary)
    coding_mode = infer_coding_mode(session_summary).value
    effective_intensity = session_intensity_override or session_persona.value
    effective_persona = (
        _UP(effective_intensity)
        if effective_intensity in ("active", "delegating", "unknown")
        else session_persona
    )
    current_task_intent = infer_task_intent(task)
    current_turn_purpose = infer_turn_purpose(task).value
    recent_verif_failures = sum(
        1 for row in apply_row_dicts if row.get("verification_passed") == 0
    )
    matched_defaults = match_default_preferences(
        session_summary=session_summary,
        current_task_intent=current_task_intent,
        stage="apply",
        recent_verification_failures=recent_verif_failures,
    )
    autonomy_derived_prefs = autonomy_prefs_to_preferences(autonomy_preferences)

    confirmed_prefs = _load_confirmed_preferences(trust_db, repo_root_str, run_session_id)

    # Session position over apply-stage turns only (consistent with n_turns).
    session_position = min(session_summary.n_turns / 20.0, 1.0)
    forced_action = force_action_from_preferences(matched_defaults)
    pushback_counts = pushback_counts_from_rows(apply_row_dicts)

    return _SessionContext(
        session_row_dicts=all_row_dicts,
        apply_row_dicts=apply_row_dicts,
        session_summary=session_summary,
        session_persona=session_persona,
        effective_intensity=effective_intensity,
        effective_persona=effective_persona,
        coding_mode=coding_mode,
        current_task_intent=current_task_intent,
        current_turn_purpose=current_turn_purpose,
        recent_verif_failures=recent_verif_failures,
        matched_defaults=matched_defaults,
        autonomy_derived_prefs=autonomy_derived_prefs,
        confirmed_prefs=confirmed_prefs,
        session_position=session_position,
        forced_action=forced_action,
        pushback_counts=pushback_counts,
    )


def _accumulate_hypothesis_evidence(
    *,
    ctx: _SessionContext,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    client: ClaudeClient | None,
) -> None:
    """Evidence side of the hypothesis bank: seed candidates, score the latest
    trace, and kick off background LLM generation. Never surfaces UI."""
    seed_candidates_from_session(
        trust_db=trust_db,
        repo_root=repo_root_str,
        session_id=run_session_id,
        session_summary=ctx.session_summary,
        pushback_counts=ctx.pushback_counts,
        recent_verification_failures=ctx.recent_verif_failures,
        inferred_persona=ctx.effective_persona,
    )

    if ctx.apply_row_dicts:
        update_evidence(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            trace=ctx.apply_row_dicts[-1],
        )

    from ..hypothesis_bank import LLM_GENERATION_INTERVAL as _NOTICER_TICK
    n_turns = ctx.session_summary.n_turns
    if n_turns > 0 and n_turns % _NOTICER_TICK == 0 and client is not None:
        from .theme import PALETTE as _P
        from rich import print as _rprint
        _rprint(
            f"[{_P['meta_italic']}]hedwig · noticing patterns…[/{_P['meta_italic']}]"
        )

    def _noticer_safe(**kwargs):
        try:
            maybe_generate_llm_hypotheses(**kwargs)
        except Exception as exc:  # daemon thread — never crash the parent
            import sys as _sys
            print(f"[noticer] background failure: {exc}", file=_sys.stderr)

    threading.Thread(
        target=_noticer_safe,
        kwargs=dict(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            session_summary=ctx.session_summary,
            turn_count=n_turns,
            client=client,
        ),
        daemon=True,
    ).start()


def _surface_ready_candidate(
    *,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
) -> None:
    """Surface the highest-confidence ready candidate and persist the
    developer's accept/reject. Behavioral-guideline candidates are written to
    the rule store on accept; everything else becomes a confirmed preference.

    Shared by both entry points (`_surface_ready_hypothesis` during apply,
    `_surface_ready_hypothesis_after_no_op` on no-op turns) — the ONLY
    difference between them is the delegating gate, applied by the caller. The
    persist-before-mark ordering here is a correctness invariant: a candidate
    must not be marked surfaced unless its backing preference row was saved,
    or it strands as 'confirmed' with nothing behind it and can never re-surface.
    """
    hypothesis = get_ready_hypothesis(
        trust_db=trust_db, repo_root=repo_root_str, session_id=run_session_id
    )
    if hypothesis is None:
        return
    if trust_db.session_has_confirmed_hypothesis(
        repo_root_str, run_session_id, driver=hypothesis.driver
    ):
        return

    # Brief pause before the hypothesis panel so it lands as a distinct moment
    # rather than scrolling past with the preceding output.
    import sys as _sys
    if _sys.stdin.isatty():
        import time as _time
        _time.sleep(0.5)
    confirmation = render_hypothesis_confirmation(hypothesis)

    # Route on the `type` stored in preference_json. The Preference object the
    # hypothesis carries doesn't include it, so read the raw JSON via the store
    # (which owns the candidate table's shape).
    pref_data: dict = {}
    try:
        raw = trust_db.ready_candidate_preference_json(repo_root_str, hypothesis.driver)
        if raw:
            pref_data = json.loads(raw)
    except Exception:
        pref_data = {}

    if pref_data.get("type", "preference") == "behavioral_guideline":
        if confirmation.confirmed:
            text = pref_data.get("text", "").strip()
            if text:
                trust_db.add_behavioral_guidelines(
                    repo_root_str, source="llm_inferred", guidelines=[text]
                )
        mark_candidate_surfaced(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            driver=hypothesis.driver,
            confirmed=confirmation.confirmed,
        )
        return  # don't save to confirmed_preferences

    payload = (
        {
            "accepted": True,
            "driver": hypothesis.driver,
            "preference": preference_to_dict(hypothesis.proposed_preference),
        }
        if confirmation.confirmed
        else {"accepted": False, "driver": hypothesis.driver}
    )
    # Persist preference first; only mark surfaced if the save succeeds.
    try:
        trust_db.save_confirmed_preference(
            repo_root=repo_root_str,
            session_id=run_session_id,
            preference_json=json.dumps(payload),
            driver=hypothesis.driver,
        )
    except Exception as exc:
        print(f"[apply] preference save failed: {exc}", file=_sys.stderr)
        return
    mark_candidate_surfaced(
        trust_db=trust_db,
        repo_root=repo_root_str,
        session_id=run_session_id,
        driver=hypothesis.driver,
        confirmed=confirmation.confirmed,
    )


def _surface_ready_hypothesis(
    *,
    ctx: _SessionContext,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
) -> None:
    """Confirmation side during apply. Gated on intensity — delegating sessions
    don't get interrupted; evidence still accumulates upstream."""
    if ctx.effective_persona.value == "delegating":
        return
    _surface_ready_candidate(
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        run_session_id=run_session_id,
    )


def _surface_ready_hypothesis_after_no_op(
    *,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
) -> None:
    """Surface a ready hypothesis when the apply path was skipped (no-op task).

    Evidence can accumulate during plan-stage pushback even when the agent
    decides no edits are needed. The normal surfacing path lives inside
    _evaluate_apply_stage, which never runs on no-op tasks — without this,
    a ready candidate gets stuck in ready_to_surface indefinitely.

    No delegating gate here: on no-op tasks we have no session context to
    infer intensity from, so we always surface if a candidate is ready.
    """
    _surface_ready_candidate(
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        run_session_id=run_session_id,
    )


def _run_hypothesis_pipeline(
    *,
    ctx: _SessionContext,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
    client: ClaudeClient | None,
) -> None:
    """Trial-Error-Explain loop: accumulate evidence, then surface a ready
    candidate if one exists. Composed of two halves so each is testable."""
    _accumulate_hypothesis_evidence(
        ctx=ctx,
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        run_session_id=run_session_id,
        client=client,
    )
    _surface_ready_hypothesis(
        ctx=ctx,
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        run_session_id=run_session_id,
    )


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
    allow_revise: bool = False,
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

    def _record(
        files: list[str],
        user_decision: str,
        *,
        response_time_ms: int | None = None,
        user_feedback_text: str | None = None,
        check_in_initiators: dict[str, str | None] | None = None,
        blast_radius_override: int | None = None,
    ) -> None:
        _record_traces(
            trust_db=trust_db,
            repo_root=repo_root_str,
            session_id=run_session_id,
            task=task,
            stage="apply",
            action_type="write_request",
            files=files,
            histories=apply_histories,
            policies=apply_policies,
            user_decision=user_decision,
            response_time_ms=response_time_ms,
            change_types={p: change_type_label(r) for p, r in apply_risk.items()},
            diff_sizes={p: r.diff_size for p, r in apply_risk.items()},
            blast_radius=blast_radius_override if blast_radius_override is not None else len(touched_files),
            existing_leases=apply_leases,
            user_feedback_text=user_feedback_text,
            check_in_initiators=check_in_initiators,
            study_context=study_context,
            model_risk_by_file={p: (r.model_risk_score, r.model_risk_rationale) for p, r in apply_risk.items()},
            is_security_sensitive_by_file={p: r.is_security_sensitive for p, r in apply_risk.items()},
        )

    def _handle_denial(feedback_text: str | None, *, intervention: bool = False) -> None:
        is_revise = bool(feedback_text and feedback_text.startswith("[revise]"))
        if is_revise:
            _stash_revise(feedback_text[len("[revise]"):].strip() or "")
            print(f"[{_THEME['attention']}]✎ narrowing scope — regenerating with your feedback.[/{_THEME['attention']}]")
        else:
            render_apply_denied(intervention=intervention)
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
    _loaded = trust_db.load_policy_model(repo_root_str)
    classifier: PolicyClassifier = _loaded if _loaded is not None else build_cold_classifier()
    if _loaded is None:
        trust_db.save_policy_model(repo_root_str, classifier)

    ctx = _build_session_context(
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        run_session_id=run_session_id,
        task=task,
        autonomy_preferences=autonomy_preferences,
        session_intensity_override=session_intensity_override,
    )

    _apply_regret_corrections(
        classifier=classifier,
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        session_row_dicts=ctx.apply_row_dicts,
        recent_apply_denials=recent_apply_denials,
    )

    _run_hypothesis_pipeline(
        ctx=ctx,
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        run_session_id=run_session_id,
        client=client,
    )


    preference_coordinator = PreferenceCoordinator(
        confirmed_prefs=ctx.confirmed_prefs,
        autonomy_derived_prefs=ctx.autonomy_derived_prefs,
        matched_defaults=ctx.matched_defaults,
        session_summary=ctx.session_summary,
        current_task_intent=ctx.current_task_intent,
        current_turn_purpose=ctx.current_turn_purpose,
        recent_verification_failures=ctx.recent_verif_failures,
        session_position=ctx.session_position,
        session_id=run_session_id,
    )

    for path in touched_files:
        history = trust_db.policy_history(repo_root_str, path, stage="apply")
        apply_histories[path] = history

        diff_size, is_new_file = change_metrics.get(path, (0, False))
        file_path = repo_root / path
        try:
            old_content = file_path.read_text()
        except (FileNotFoundError, IsADirectoryError, OSError):
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
        if client is not None and should_review(risk=risk, history=history):
            model_score, model_rationale = assess_risk_via_model(
                file_path=path,
                diff_or_content=new_content,
                file_context=old_content,
                agent_client=client,
            )
            risk = RiskSignals(
                change_pattern=risk.change_pattern,
                blast_radius=risk.blast_radius,
                is_security_sensitive=risk.is_security_sensitive,
                is_new_file=risk.is_new_file,
                diff_size=risk.diff_size,
                model_risk_score=model_score,
                model_risk_rationale=model_rationale,
            )
        apply_risk[path] = risk

        constraint = apply_constraints.get(path)
        lease = active_apply.get(path)
        pre = _resolve_pre_scorer(constraint=constraint, lease=lease, access_type="write")
        if pre is not None:
            decision, lease_label, outcome = pre
            apply_leases[path] = lease_label
            apply_policies[path] = decision
            if outcome == "deny":
                denied_apply.append(path)
            elif outcome == "check_in":
                prompt_required = True
            continue
        apply_leases[path] = None

        if config.adaptive_policy_enabled:
            proceed_threshold, flag_threshold = adjusted_policy_thresholds(
                profile.proceed_threshold,
                profile.flag_threshold,
                autonomy_preferences,
                file_path=path,
                model_checkin_approval_rate=model_checkin_rate,
                model_checkin_total=model_checkin_total,
                session_intensity=ctx.effective_intensity,
                coding_mode=ctx.coding_mode,
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
            if client is not None and is_borderline(decision.score, proceed_threshold):
                _vote, _vote_rationale = ask_model_to_vote(
                    file_path=path,
                    diff_or_content=new_content,
                    file_context=old_content,
                    agent_client=client,
                )
                if _vote is not None:
                    _new_score = decision.score + (-_BORDERLINE_MAX_SHIFT if _vote else _BORDERLINE_MAX_SHIFT)
                    decision = PolicyDecision(
                        action=_bucket(_new_score, proceed_threshold, flag_threshold),
                        score=_new_score,
                        reasons=decision.reasons + (
                            f"model vote: {'pause' if _vote else 'proceed'} — {_vote_rationale}",
                        ),
                    )
        else:
            decision = PolicyDecision(
                action="check_in",
                score=0.0,
                reasons=("adaptive policy disabled",),
            )
        decision = preference_coordinator.apply_to_decision(
            decision=decision,
            file_path=path,
            risk=risk,
        ).decision

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
        apply_risk=apply_risk,
        task_intent=getattr(ctx.current_task_intent, "value", None),
        persona=getattr(ctx.session_persona, "value", None),
        trust_db=trust_db,
        repo_root=repo_root_str,
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
        _record(touched_files, "deny")
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
            session_row_dicts=ctx.session_row_dicts,
            verification_failure_rates=verification_failure_rates,
            remember=remember,
            scope_budget_files=config.scope_budget_files,
            allow_revise=allow_revise,
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
        if auto_files:
            _record(auto_files, "auto_approve" if approved else "deny")
        prompted_decision = (
            "approve_and_remember" if approved and remembered
            else ("approve" if approved else "deny")
        )
        _record(
            check_in_files,
            prompted_decision,
            response_time_ms=response_time_ms,
            user_feedback_text=apply_feedback,
            check_in_initiators=_policy_checkin_initiators(check_in_files, apply_policies),
        )
        feedback.note_decision(
            approved,
            change_patterns=[change_type_label(r) for r in apply_risk.values()] if not approved else None,
            response_time_ms=response_time_ms,
            feedback_text=apply_feedback,
        )
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
        _feedback_thread = threading.Thread(
            target=_apply_feedback_learning,
            kwargs=dict(
                trust_db=trust_db,
                repo_root=repo_root_str,
                session=session,
                feedback_text=apply_feedback,
                client=client,
                guidance_prefix="Write decision guidance",
            ),
            daemon=True,
        )
        _feedback_thread.start()
        if not approved:
            _handle_denial(apply_feedback)
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

    _any_soft_checkin = (
        (ctx.forced_action is not None and ctx.forced_action.value == "soft_checkin")
        or bool(flagged_auto_files)
    )
    if _any_soft_checkin:
        outcome = render_soft_checkin_gate(
            touched_files=touched_files,
            apply_policies=apply_policies,
        )
        if outcome.intervened:
            approved, remembered, apply_feedback = _prompt_approval(
                "apply", touched_files, remember, diff_already_shown=False,
                allow_revise=allow_revise,
            )
            if not approved:
                trust_db.record_decision(
                    repo_root_str, task, "apply",
                    approved=False, remembered=False,
                    planned_files=touched_files, touched_files=touched_files,
                )
                _record(
                    touched_files, "deny",
                    user_feedback_text=apply_feedback,
                    blast_radius_override=max(
                        (r.blast_radius for r in apply_risk.values()), default=0
                    ),
                )
                _handle_denial(apply_feedback, intervention=True)
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
    _record(touched_files, user_decision)
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
        except (FileNotFoundError, IsADirectoryError, OSError):
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
            except (FileNotFoundError, IsADirectoryError, OSError):
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
            except OSError:
                pass
