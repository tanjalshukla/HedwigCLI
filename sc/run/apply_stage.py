from __future__ import annotations

"""Apply-stage policy decisions and write/verification execution for `hw run`.

The apply cascade follows the same four steps as the read cascade in
``read_stage.py`` — hard constraints → leases → PolicyScorer → preference
override — and shares ``helpers._resolve_pre_scorer`` and
``helpers._policy_decision_for_file`` for the first three. What this stage
adds on top of that shared cascade and the read stage does not:

* **Regret corrections** — replay regret events as negative classifier
  signal once per trace_id (``_apply_regret_corrections``).
* **Hypothesis pipeline** — seed/score/surface/generate
  (``_run_hypothesis_pipeline``).
* **Classifier online updates** — every developer decision becomes a
  ``partial_fit`` call (``_update_classifier``).
* **Atomic write + verification** — apply the patch, run hooks, persist
  verification outcome (``_apply_updates_and_verify``).
* **Stricter thresholds** and the full ``PreferenceCoordinator`` override
  (defaults + autonomy-derived + confirmed prefs).

Unifying the read and apply cascades into one parameterized module is
parked post-conference (see BRAINSTORM.md): the shared work already lives
in ``helpers``, and the differences listed above are intentional asymmetries
between read-stage and apply-stage authority.
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
from ..features import RiskSignals, assess_risk, change_type_label
from ..model_risk import (
    assess_risk_via_model,
    ask_model_to_vote,
    is_borderline,
    should_review,
    _BORDERLINE_MAX_SHIFT,  # noqa: F401 — used in borderline vote nudge below
)
from ..ml_policy import PolicyClassifier, build_cold_classifier
from ..policy import PolicyDecision
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
from .traces import _policy_checkin_initiators, _record_traces
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
        # Skip regrets already applied to this classifier instance. Without
        # this guard, every call re-applies all prior regrets, producing O(N)
        # spurious negative signals that can fully reverse real approval history.
        regret_key = event.auto_approve_trace_id
        if regret_key in classifier._corrected_regret_ids:
            continue

        # Reconstruct a PolicyInput from the auto-approve trace that caused
        # the regret. diff_size and blast_radius are stored in decision_traces.
        regret_row = next(
            (r for r in session_row_dicts if r.get("id") == event.auto_approve_trace_id),
            None,
        )
        if regret_row is None:
            continue
        history = trust_db.policy_history(repo_root_str, event.file_path, stage="apply")
        # change_type is stored by change_type_label() with a legacy "new_file:"
        # prefix when the action created a file. Split it back into the bare
        # change_pattern + is_new_file flag the scorer expects — otherwise a
        # prefixed pattern misses _PATTERN_RISK entirely (scores as a generic
        # change) AND the new-file penalty is lost, under-weighting the very
        # regrets that matter most (a reverted new-file edit).
        stored_change_type = str(regret_row.get("change_type") or "general_change")
        regret_is_new_file = stored_change_type.startswith("new_file:")
        change_pattern = (
            stored_change_type.split(":", 1)[-1] if regret_is_new_file else stored_change_type
        )
        pi = PolicyInput(
            prior_approvals=max(0.0, history.effective_approvals - 1),
            prior_denials=history.denials,
            avg_response_ms=history.avg_response_ms,
            avg_edit_distance=history.avg_edit_distance or 0.0,
            diff_size=int(regret_row.get("diff_size") or 0),
            blast_radius=int(regret_row.get("blast_radius") or 1),
            is_new_file=regret_is_new_file,
            is_security_sensitive=False,
            change_pattern=change_pattern,
            recent_denials=recent_apply_denials,
            files_in_action=1,
        )
        # count_sample=False: regret replay is a corrective gradient, not a
        # new developer decision — it must not push the classifier across the
        # MIN_SAMPLES_FOR_LEARNED threshold or distort the learned-vs-heuristic
        # transition.
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
        pi = PolicyInput.from_signals(
            history,
            risk,
            recent_denials=recent_apply_denials,
            files_in_action=len(files),
        )
        if is_rubber_stamp:
            # Rubber-stamp half-weight: one approve + one deny, net zero push.
            # count_sample=False on the second call so this counts as 1 decision.
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


def _surface_ready_hypothesis(
    *,
    ctx: _SessionContext,
    trust_db: TrustDB,
    repo_root_str: str,
    run_session_id: str,
) -> None:
    """Confirmation side: surface the highest-confidence ready candidate and
    persist the developer's accept/reject. Gated on intensity — delegating
    sessions don't get interrupted; evidence still accumulates upstream."""
    if ctx.effective_persona.value == "delegating":
        return

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

    # Route based on the type stored in preference_json.
    # hypothesis only carries proposed_preference (a Preference object), not the raw JSON,
    # so fetch preference_json directly from the DB to read the type field.
    _pref_data: dict = {}
    try:
        with trust_db._connect() as _conn:
            _row = _conn.execute(
                "SELECT preference_json FROM hypothesis_candidates "
                "WHERE repo_root = ? AND driver = ? AND status = 'ready_to_surface' LIMIT 1",
                (repo_root_str, hypothesis.driver),
            ).fetchone()
        if _row and _row["preference_json"]:
            _pref_data = json.loads(_row["preference_json"])
    except Exception:
        _pref_data = {}

    candidate_type = _pref_data.get("type", "preference")

    if candidate_type == "behavioral_guideline":
        if confirmation.confirmed:
            _text = _pref_data.get("text", "").strip()
            if _text:
                trust_db.add_behavioral_guidelines(
                    repo_root_str,
                    source="llm_inferred",
                    guidelines=[_text],
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
    # Persist preference first; only mark the candidate surfaced if the save
    # succeeds. Otherwise the candidate would be left as 'confirmed' with no
    # backing preference row and re-surfacing would be impossible.
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
    hypothesis = get_ready_hypothesis(
        trust_db=trust_db, repo_root=repo_root_str, session_id=run_session_id
    )
    if hypothesis is None:
        return
    if trust_db.session_has_confirmed_hypothesis(
        repo_root_str, run_session_id, driver=hypothesis.driver
    ):
        return
    import sys as _sys
    if _sys.stdin.isatty():
        import time as _time
        _time.sleep(0.5)
    confirmation = render_hypothesis_confirmation(hypothesis)

    _pref_data_noop: dict = {}
    try:
        with trust_db._connect() as _conn:
            _row_noop = _conn.execute(
                "SELECT preference_json FROM hypothesis_candidates "
                "WHERE repo_root = ? AND driver = ? AND status = 'ready_to_surface' LIMIT 1",
                (repo_root_str, hypothesis.driver),
            ).fetchone()
        if _row_noop and _row_noop["preference_json"]:
            _pref_data_noop = json.loads(_row_noop["preference_json"])
    except Exception:
        _pref_data_noop = {}

    candidate_type_noop = _pref_data_noop.get("type", "preference")

    if candidate_type_noop == "behavioral_guideline":
        if confirmation.confirmed:
            _text_noop = _pref_data_noop.get("text", "").strip()
            if _text_noop:
                trust_db.add_behavioral_guidelines(
                    repo_root_str,
                    source="llm_inferred",
                    guidelines=[_text_noop],
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

    def _risk_labels() -> dict[str, str | None]:
        return {p: change_type_label(r) for p, r in apply_risk.items()}

    def _risk_diff_sizes() -> dict[str, int | None]:
        return {p: r.diff_size for p, r in apply_risk.items()}

    def _risk_model_review() -> dict[str, tuple[float, str]]:
        return {
            p: (r.model_risk_score, r.model_risk_rationale)
            for p, r in apply_risk.items()
        }
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

    # --- Phase 1: session context ---
    # All session-level signals computed once, before any per-file work.
    ctx = _build_session_context(
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        run_session_id=run_session_id,
        task=task,
        autonomy_preferences=autonomy_preferences,
        session_intensity_override=session_intensity_override,
    )

    # --- Phase 2: regret corrections ---
    # Regret detection is only meaningful over apply-stage decisions.
    _apply_regret_corrections(
        classifier=classifier,
        trust_db=trust_db,
        repo_root_str=repo_root_str,
        session_row_dicts=ctx.apply_row_dicts,
        recent_apply_denials=recent_apply_denials,
    )

    # --- Phase 3: hypothesis bank ---
    # Seed, score evidence, surface if confident, kick off LLM generation.
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

    # Counter for the status display — tracks how many reviewer calls fired
    # this turn. No cap: should_review() is the gate; silently skipping a
    # security-sensitive file because of an arbitrary budget is worse than
    # the cost of one extra call.
    reviewer_calls = 0

    # Score each touched file independently, then aggregate to one approval decision.
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
        # Adversarial-reviewer pass — augments risk with an advisory
        # model_risk_score. Apply-stage only (read-stage doesn't use this).
        # Failures fall back to (0.5, "") so the deterministic signals stay
        # authoritative; never a veto, never a loosener.
        if client is not None and should_review(risk=risk, history=history):
            model_score, model_rationale = assess_risk_via_model(
                file_path=path,
                diff_or_content=new_content,
                file_context=old_content,
                agent_client=client,
            )
            reviewer_calls += 1
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
            # outcome == "allow" / "lease" needs no per-file side effect at
            # the apply stage: the decision itself is enough, and there is no
            # symmetric "auto_apply" list (writes always rendered through
            # render_apply_auto_approved later).
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
            # Borderline model vote — only when the scorer is genuinely
            # uncertain (score within _BORDERLINE_BAND of the proceed
            # threshold). Counts against the reviewer budget so we don't
            # add a second call on top of an existing adversarial-reviewer
            # call for the same file. On any Bedrock failure the vote is
            # None and the decision is left unchanged.
            if (
                client is not None
                and is_borderline(decision.score, proceed_threshold)
            ):
                new_content = updates.get(path, "")
                old_content_for_vote: str
                try:
                    old_content_for_vote = (repo_root / path).read_text()
                except (FileNotFoundError, OSError):
                    old_content_for_vote = ""
                _vote, _vote_rationale = ask_model_to_vote(
                    file_path=path,
                    diff_or_content=new_content,
                    file_context=old_content_for_vote,
                    agent_client=client,
                )
                reviewer_calls += 1
                if _vote is not None:
                    # Nudge score toward or away from the threshold by
                    # _BORDERLINE_MAX_SHIFT, then re-bucket. The shift is
                    # additive — deterministic signals still dominate.
                    _nudge = -_BORDERLINE_MAX_SHIFT if _vote else _BORDERLINE_MAX_SHIFT
                    _new_score = decision.score + _nudge
                    from ..policy import _bucket
                    _new_action = _bucket(_new_score, proceed_threshold, flag_threshold)
                    _vote_reason = (
                        f"model vote: pause — {_vote_rationale}"
                        if _vote
                        else f"model vote: proceed — {_vote_rationale}"
                    )
                    decision = PolicyDecision(
                        action=_new_action,
                        score=_new_score,
                        reasons=decision.reasons + (_vote_reason,),
                    )
        else:
            decision = PolicyDecision(
                action="check_in",
                score=0.0,
                reasons=("adaptive policy disabled",),
            )
        # Per-file preference resolution: tighten or loosen the scorer's
        # decision against built-in defaults, AutonomyPreferences-derived
        # preferences, and session-confirmed preferences. Hard constraints
        # never reach this point (they `continue` above with score -1000/-500).
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
            model_risk_by_file=_risk_model_review(),
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
                model_risk_by_file=_risk_model_review(),
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
            model_risk_by_file=_risk_model_review(),
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
        import threading as _threading
        _feedback_thread = _threading.Thread(
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
            # 'v' (revise scope) returns from _prompt_approval as a deny variant
            # with a [revise] prefix on the feedback. Stash the feedback so the
            # REPL outer loop can re-issue the task as a narrow-scope follow-up
            # instead of making the visitor retype it. Apply itself still exits
            # (code 0) — the actual loop-back happens in run_repl.
            _is_revise = bool(apply_feedback and apply_feedback.startswith("[revise]"))
            if _is_revise:
                from ..run.theme import PALETTE as _PAL_R
                from .revise_state import stash as _stash_revise
                # Strip the [revise] tag — keep only the developer's note.
                _note = apply_feedback[len("[revise]"):].strip()
                _stash_revise(_note or "")
                print(
                    f"[{_PAL_R['attention']}]✎ narrowing scope — regenerating with your feedback.[/{_PAL_R['attention']}]"
                )
            else:
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

    # If any preference (default or per-file confirmed) triggered a soft
    # check-in, render the non-blocking panel. _forced_action is the session-
    # level default check; also gate on flagged_auto_files (which are set when
    # a per-file SOFT_CHECKIN preference upgraded a proceed to proceed_flag).
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
            from .ui import _prompt_approval as _full_prompt
            approved, remembered, apply_feedback = _full_prompt(
                "apply", touched_files, remember, diff_already_shown=False,
                allow_revise=allow_revise,
            )
            if not approved:
                _is_revise = bool(apply_feedback and apply_feedback.startswith("[revise]"))
                trust_db.record_decision(
                    repo_root_str, task, "apply",
                    approved=False, remembered=False,
                    planned_files=touched_files, touched_files=touched_files,
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
                    blast_radius=max((r.blast_radius for r in apply_risk.values()), default=0),
                    existing_leases=apply_leases,
                    user_feedback_text=apply_feedback,
                    study_context=study_context,
                )
                if _is_revise:
                    from ..run.theme import PALETTE as _PAL_R
                    from .revise_state import stash as _stash_revise
                    _note = apply_feedback[len("[revise]"):].strip()
                    _stash_revise(_note or "")
                    print(
                        f"[{_PAL_R['attention']}]✎ narrowing scope — regenerating with your feedback.[/{_PAL_R['attention']}]"
                    )
                else:
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
        model_risk_by_file=_risk_model_review(),
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
