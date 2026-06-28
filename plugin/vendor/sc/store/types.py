from __future__ import annotations

"""Typed shapes for store row results.

Sqlite returns ``sqlite3.Row`` objects which expose ``__getitem__`` access
identical to a ``Mapping[str, Any]``. Wherever rows are converted to dicts
(``dict(row)``), the result is conceptually a ``DecisionTraceRow`` — every
field is optional because callers select different column subsets.
"""

from typing import Any, TypedDict


class DecisionTraceRow(TypedDict, total=False):
    id: int
    repo_root: str
    session_id: str
    task: str
    stage: str
    action_type: str
    file_path: str
    change_type: str | None
    diff_size: int | None
    blast_radius: int | None
    existing_lease: int
    lease_type: str | None
    prior_approvals: int
    prior_denials: int
    policy_action: str
    policy_score: float
    policy_reasons_json: str | None
    user_decision: str
    response_time_ms: int | None
    review_duration_seconds: float | None
    rubber_stamp: int | None
    edit_distance: float | None
    user_feedback_text: str | None
    verification_passed: int | None
    verification_checks_json: str | None
    expected_behavior: str | None
    model_confidence_self_report: float | None
    model_assumptions_json: str | None
    check_in_initiator: str | None
    participant_id: str | None
    study_run_id: str | None
    study_task_id: str | None
    autonomy_mode: str | None
    pushback_type: str | None
    scorer_uncertainty: float | None
    turn_purpose: str | None
    model_risk_score: float | None
    model_risk_rationale: str | None
    is_security_sensitive: int | None
    created_at: int


class PlanRevisionRow(TypedDict, total=False):
    id: int
    repo_root: str
    session_id: str
    task: str
    revision_round: int
    plan_hash: str
    intent_json: str
    reasons_json: str
    developer_feedback: str | None
    approved: int
    participant_id: str | None
    study_run_id: str | None
    study_task_id: str | None
    autonomy_mode: str | None
    created_at: int


# sqlite3.Row supports both index- and key-based access; treat as Any-keyed
# read-only mapping for type purposes when callers don't convert via dict().
SqliteRow = Any
