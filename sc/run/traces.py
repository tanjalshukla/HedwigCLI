from __future__ import annotations

"""Trace recording helpers used by run flow."""

from ..policy import PolicyDecision
from ..preference_inference import classify_pushback, infer_turn_purpose
from ..trust_db import PolicyHistory, TrustDB
from ..verification import VerificationResult
from .helpers import StudyContext


def _record_traces(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
    task: str,
    stage: str,
    action_type: str,
    files: list[str],
    histories: dict[str, PolicyHistory],
    policies: dict[str, PolicyDecision],
    user_decision: str,
    response_time_ms: int | None,
    change_types: dict[str, str | None],
    diff_sizes: dict[str, int | None],
    blast_radius: int,
    existing_leases: dict[str, str | None],
    user_feedback_text: str | None = None,
    verification_result: VerificationResult | None = None,
    model_confidence_by_file: dict[str, float | None] | None = None,
    model_assumptions_by_file: dict[str, list[str] | None] | None = None,
    check_in_initiators: dict[str, str | None] | None = None,
    study_context: StudyContext | None = None,
) -> None:
    for path in files:
        history = histories[path]
        policy = policies[path]
        lease_type = existing_leases.get(path)
        initiator = check_in_initiators.get(path) if check_in_initiators else None
        model_confidence = (
            model_confidence_by_file.get(path)
            if model_confidence_by_file
            else None
        )
        model_assumptions = (
            model_assumptions_by_file.get(path)
            if model_assumptions_by_file
            else None
        )
        # SWE-chat-grounded taxonomy signals, computed at record time so they're
        # queryable alongside the raw trace fields they derive from.
        pushback_type = classify_pushback(
            user_decision=user_decision,
            edit_distance=None,  # edit_distance isn't threaded through here yet
            user_feedback_text=user_feedback_text,
        ).value
        # Turn purpose — what the task is *for*, orthogonal to pushback_type.
        # Used downstream to avoid counting context-provision turns as pushback.
        turn_purpose = infer_turn_purpose(task).value
        scorer_uncertainty = (
            abs(policy.score - 0.5) if policy.score is not None else None
        )
        trust_db.record_trace(
            repo_root=repo_root,
            session_id=session_id,
            task=task,
            stage=stage,
            action_type=action_type,
            file_path=path,
            change_type=change_types.get(path),
            diff_size=diff_sizes.get(path),
            blast_radius=blast_radius,
            existing_lease=lease_type is not None,
            lease_type=lease_type,
            prior_approvals=history.approvals,
            prior_denials=history.denials,
            policy_action=policy.action,
            policy_score=policy.score,
            policy_reasons=policy.reasons,
            user_decision=user_decision,
            response_time_ms=response_time_ms,
            edit_distance=None,
            user_feedback_text=user_feedback_text,
            verification_passed=verification_result.passed if verification_result else None,
            verification_checks_json=verification_result.checks_json() if verification_result else None,
            expected_behavior=verification_result.expected_behavior if verification_result else None,
            model_confidence_self_report=model_confidence,
            model_assumptions=model_assumptions,
            check_in_initiator=initiator,
            participant_id=study_context.participant_id if study_context else None,
            study_run_id=study_context.study_run_id if study_context else None,
            study_task_id=study_context.study_task_id if study_context else None,
            autonomy_mode=study_context.autonomy_mode if study_context else None,
            pushback_type=pushback_type,
            scorer_uncertainty=scorer_uncertainty,
            turn_purpose=turn_purpose,
        )


def _policy_checkin_initiators(
    files: list[str], policies: dict[str, PolicyDecision]
) -> dict[str, str | None]:
    return {
        path: ("policy" if policies.get(path) and policies[path].action == "check_in" else None)
        for path in files
    }
