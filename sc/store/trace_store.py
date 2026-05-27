from __future__ import annotations

"""TraceStoreMixin — decision traces, plan revisions, verification, and calibration analytics.

Methods: record_decision, record_trace, policy_history, recent_denials,
verification_failure_rate, model_confidence_stats, list_traces, trace_count,
trace_by_id, session_traces, clear_traces, clear_traces_for_file,
record_plan_revision, clear_plan_revisions, latest_session_id,
session_plan_revisions, plan_revision_summary, attach_verification_result,
verification_summary, session_verification_status, checkin_usefulness_summary,
access_stats, checkin_calibration, model_checkin_calibration, trust_summary.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from ..trust_db import (
        AccessStats,
        CheckInCalibration,
        CheckInUsefulnessSummary,
        ModelConfidenceStats,
        PlanRevisionSummary,
        PolicyHistory,
        TrustSummary,
    )


class TraceStoreMixin:
    # _connect and dataclasses are provided by TrustDB.

    def record_decision(
        self,
        repo_root: str,
        task: str,
        decision_type: str,
        approved: bool,
        remembered: bool,
        planned_files: Iterable[str],
        touched_files: Iterable[str] | None = None,
    ) -> None:
        now = int(time.time())
        planned_json = json.dumps(list(planned_files))
        touched_json = json.dumps(list(touched_files)) if touched_files is not None else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO decisions (
                    repo_root, task, decision_type, approved, remembered,
                    planned_files_json, touched_files_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_root,
                    task,
                    decision_type,
                    1 if approved else 0,
                    1 if remembered else 0,
                    planned_json,
                    touched_json,
                    now,
                ),
            )

    def record_trace(
        self,
        repo_root: str,
        session_id: str,
        task: str,
        stage: str,
        action_type: str,
        file_path: str,
        change_type: str | None,
        diff_size: int | None,
        blast_radius: int | None,
        existing_lease: bool,
        lease_type: str | None,
        prior_approvals: int,
        prior_denials: int,
        policy_action: str,
        policy_score: float,
        user_decision: str,
        policy_reasons: Iterable[str] | None = None,
        response_time_ms: int | None = None,
        review_duration_seconds: float | None = None,
        rubber_stamp: bool | None = None,
        edit_distance: float | None = None,
        user_feedback_text: str | None = None,
        verification_passed: bool | None = None,
        verification_checks_json: str | None = None,
        expected_behavior: str | None = None,
        model_confidence_self_report: float | None = None,
        model_assumptions: Iterable[str] | None = None,
        check_in_initiator: str | None = None,
        participant_id: str | None = None,
        study_run_id: str | None = None,
        study_task_id: str | None = None,
        autonomy_mode: str | None = None,
        pushback_type: str | None = None,
        scorer_uncertainty: float | None = None,
        turn_purpose: str | None = None,
        model_risk_score: float | None = None,
        model_risk_rationale: str | None = None,
    ) -> None:
        now = int(time.time())
        if review_duration_seconds is None and response_time_ms is not None:
            review_duration_seconds = round(max(response_time_ms, 0) / 1000.0, 3)
        if rubber_stamp is None and review_duration_seconds is not None:
            rubber_stamp = review_duration_seconds < 5.0
        assumptions_json = (
            json.dumps([item for item in model_assumptions if item])
            if model_assumptions is not None
            else None
        )
        reasons_json = (
            json.dumps([item for item in policy_reasons if item])
            if policy_reasons is not None
            else None
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO decision_traces (
                    repo_root, session_id, task, stage, action_type, file_path,
                    change_type, diff_size, blast_radius, existing_lease, lease_type,
                    prior_approvals, prior_denials, policy_action, policy_score, policy_reasons_json,
                    user_decision, response_time_ms, review_duration_seconds, rubber_stamp,
                    edit_distance, user_feedback_text,
                    verification_passed, verification_checks_json, expected_behavior,
                    model_confidence_self_report, model_assumptions_json,
                    check_in_initiator, participant_id, study_run_id, study_task_id, autonomy_mode,
                    pushback_type, scorer_uncertainty, turn_purpose,
                    model_risk_score, model_risk_rationale, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_root,
                    session_id,
                    task,
                    stage,
                    action_type,
                    file_path,
                    change_type,
                    diff_size,
                    blast_radius,
                    1 if existing_lease else 0,
                    lease_type,
                    prior_approvals,
                    prior_denials,
                    policy_action,
                    policy_score,
                    reasons_json,
                    user_decision,
                    response_time_ms,
                    review_duration_seconds,
                    None if rubber_stamp is None else (1 if rubber_stamp else 0),
                    edit_distance,
                    user_feedback_text,
                    None if verification_passed is None else (1 if verification_passed else 0),
                    verification_checks_json,
                    expected_behavior,
                    model_confidence_self_report,
                    assumptions_json,
                    check_in_initiator,
                    participant_id,
                    study_run_id,
                    study_task_id,
                    autonomy_mode,
                    pushback_type,
                    scorer_uncertainty,
                    turn_purpose,
                    model_risk_score,
                    model_risk_rationale,
                    now,
                ),
            )

    def policy_history(self, repo_root: str, file_path: str, stage: str) -> "PolicyHistory":
        from ..trust_db import PolicyHistory
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_decision, response_time_ms, edit_distance, rubber_stamp
                FROM decision_traces
                WHERE repo_root = ? AND file_path = ? AND stage = ?
                """,
                (repo_root, file_path, stage),
            ).fetchall()

        approvals = 0
        denials = 0
        effective_approvals = 0.0
        rubber_stamp_approvals = 0
        response_values: list[int] = []
        edit_values: list[float] = []

        for row in rows:
            decision = row["user_decision"]
            if decision in {
                "approve",
                "approve_and_remember",
                "auto_approve",
                "auto_approve_flag",
                "auto_approve_lease",
                "auto_approve_read_lease",
            }:
                approvals += 1
                is_rubber = bool(row["rubber_stamp"] == 1)
                if is_rubber:
                    effective_approvals += 0.5
                    rubber_stamp_approvals += 1
                else:
                    effective_approvals += 1.0
            elif decision == "deny":
                denials += 1

            response_ms = row["response_time_ms"]
            if response_ms is not None:
                response_values.append(int(response_ms))

            edit_distance = row["edit_distance"]
            if edit_distance is not None:
                edit_values.append(float(edit_distance))

        avg_response_ms: float | None = None
        if response_values:
            avg_response_ms = sum(response_values) / len(response_values)

        avg_edit_distance: float | None = None
        if edit_values:
            avg_edit_distance = sum(edit_values) / len(edit_values)

        return PolicyHistory(
            approvals=approvals,
            denials=denials,
            effective_approvals=effective_approvals,
            rubber_stamp_approvals=rubber_stamp_approvals,
            avg_response_ms=avg_response_ms,
            avg_edit_distance=avg_edit_distance,
        )

    def recent_denials(
        self,
        repo_root: str,
        session_id: str,
        stage: str,
        window_seconds: int = 3600,
    ) -> int:
        now = int(time.time())
        cutoff = now - max(window_seconds, 0)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM decision_traces
                WHERE repo_root = ? AND session_id = ? AND stage = ?
                  AND user_decision = 'deny' AND created_at >= ?
                """,
                (repo_root, session_id, stage, cutoff),
            ).fetchone()
        if row is None:
            return 0
        return int(row["c"] or 0)

    def verification_failure_rate(
        self,
        repo_root: str,
        file_path: str,
        *,
        stage: str = "apply",
        limit: int = 50,
    ) -> float | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT verification_passed
                FROM decision_traces
                WHERE repo_root = ? AND file_path = ? AND stage = ?
                  AND verification_passed IS NOT NULL
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (repo_root, file_path, stage, max(limit, 1)),
            ).fetchall()
        if not rows:
            return None
        failures = sum(1 for row in rows if int(row["verification_passed"]) == 0)
        return failures / len(rows)

    def model_confidence_stats(
        self,
        repo_root: str,
        *,
        file_path: str | None = None,
        limit: int = 50,
    ) -> "ModelConfidenceStats":
        from ..trust_db import ModelConfidenceStats
        where = ["repo_root = ?", "model_confidence_self_report IS NOT NULL"]
        params: list[object] = [repo_root]
        if file_path:
            where.append("(file_path = ? OR file_path = '__session__')")
            params.append(file_path)
        query = (
            "SELECT model_confidence_self_report FROM decision_traces "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        params.append(max(limit, 1))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        if not rows:
            return ModelConfidenceStats(average=None, samples=0)
        values = [float(row["model_confidence_self_report"]) for row in rows]
        return ModelConfidenceStats(average=(sum(values) / len(values)), samples=len(values))

    def list_traces(self, repo_root: str, limit: int = 50) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, session_id, stage, action_type, file_path, policy_action, policy_score, policy_reasons_json,
                    user_decision, response_time_ms, diff_size, blast_radius,
                    review_duration_seconds, rubber_stamp,
                    user_feedback_text, verification_passed, expected_behavior,
                    model_confidence_self_report, model_assumptions_json,
                    check_in_initiator, pushback_type, turn_purpose,
                    model_risk_score, model_risk_rationale,
                    participant_id, study_run_id, study_task_id, autonomy_mode, created_at
                FROM decision_traces
                WHERE repo_root = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (repo_root, max(limit, 1)),
            ).fetchall()
        return rows

    def trace_count(self, repo_root: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM decision_traces WHERE repo_root = ?",
                (repo_root,),
            ).fetchone()
        if row is None:
            return 0
        return int(row["c"] or 0)

    def trace_by_id(self, repo_root: str, trace_id: int) -> sqlite3.Row | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM decision_traces
                WHERE repo_root = ? AND id = ?
                """,
                (repo_root, trace_id),
            ).fetchone()
        return row

    def session_traces(self, repo_root: str, session_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, session_id, stage, action_type, file_path, change_type, policy_action, policy_score,
                    user_decision, response_time_ms, review_duration_seconds, rubber_stamp,
                    edit_distance, user_feedback_text, check_in_initiator, pushback_type, turn_purpose,
                    diff_size, blast_radius, verification_passed, task,
                    participant_id, study_run_id, study_task_id, autonomy_mode, created_at
                FROM decision_traces
                WHERE repo_root = ? AND session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (repo_root, session_id),
            ).fetchall()
        return rows

    def clear_traces(self, repo_root: str) -> int:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM decision_traces WHERE repo_root = ?",
                (repo_root,),
            )
        return int(result.rowcount)

    def clear_traces_for_file(self, repo_root: str, file_path: str) -> int:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM decision_traces WHERE repo_root = ? AND file_path = ?",
                (repo_root, file_path),
            )
        return int(result.rowcount)

    def clear_plan_revisions(self, repo_root: str) -> int:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM plan_revisions WHERE repo_root = ?",
                (repo_root,),
            )
        return int(result.rowcount)

    def record_plan_revision(
        self,
        *,
        repo_root: str,
        session_id: str,
        task: str,
        revision_round: int,
        plan_hash: str,
        intent_json: str,
        reasons: Iterable[str],
        developer_feedback: str | None,
        approved: bool,
        participant_id: str | None = None,
        study_run_id: str | None = None,
        study_task_id: str | None = None,
        autonomy_mode: str | None = None,
    ) -> None:
        now = int(time.time())
        reasons_json = json.dumps(list(reasons))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plan_revisions (
                    repo_root, session_id, task, revision_round, plan_hash, intent_json,
                    reasons_json, developer_feedback, approved,
                    participant_id, study_run_id, study_task_id, autonomy_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_root,
                    session_id,
                    task,
                    revision_round,
                    plan_hash,
                    intent_json,
                    reasons_json,
                    developer_feedback,
                    1 if approved else 0,
                    participant_id,
                    study_run_id,
                    study_task_id,
                    autonomy_mode,
                    now,
                ),
            )

    def latest_session_id(self, repo_root: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_id
                FROM decision_traces
                WHERE repo_root = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (repo_root,),
            ).fetchone()
        return None if row is None else str(row["session_id"])

    def session_plan_revisions(self, repo_root: str, session_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, session_id, task, revision_round, plan_hash, intent_json,
                    reasons_json, developer_feedback, approved,
                    participant_id, study_run_id, study_task_id, autonomy_mode, created_at
                FROM plan_revisions
                WHERE repo_root = ? AND session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (repo_root, session_id),
            ).fetchall()
        return rows

    def plan_revision_summary(self, repo_root: str) -> "PlanRevisionSummary":
        from ..trust_db import PlanRevisionSummary
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN approved = 1 THEN 1 ELSE 0 END) AS approved,
                    SUM(CASE WHEN approved = 0 AND developer_feedback IS NOT NULL AND TRIM(developer_feedback) != '' THEN 1 ELSE 0 END) AS revisions_requested,
                    SUM(CASE WHEN approved = 0 AND (developer_feedback IS NULL OR TRIM(developer_feedback) = '') THEN 1 ELSE 0 END) AS denied
                FROM plan_revisions
                WHERE repo_root = ?
                """,
                (repo_root,),
            ).fetchone()
        if row is None:
            return PlanRevisionSummary(total=0, approved=0, revisions_requested=0, denied=0)
        return PlanRevisionSummary(
            total=int(row["total"] or 0),
            approved=int(row["approved"] or 0),
            revisions_requested=int(row["revisions_requested"] or 0),
            denied=int(row["denied"] or 0),
        )

    def attach_verification_result(
        self,
        *,
        repo_root: str,
        session_id: str,
        files: Iterable[str],
        verification_passed: bool,
        verification_checks_json: str,
        expected_behavior: str,
    ) -> None:
        files_list = list(dict.fromkeys(files))
        if not files_list:
            return
        # Update only the most recent trace per file (by max id within this
        # session). A broad UPDATE without this guard overwrites all prior
        # traces for a file, contaminating historical verification_failure_rate.
        with self._connect() as conn:
            for file_path in files_list:
                row = conn.execute(
                    """
                    SELECT id FROM decision_traces
                    WHERE repo_root = ? AND session_id = ? AND stage = 'apply'
                      AND file_path = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (repo_root, session_id, file_path),
                ).fetchone()
                if row is None:
                    continue
                conn.execute(
                    """
                    UPDATE decision_traces
                    SET verification_passed = ?,
                        verification_checks_json = ?,
                        expected_behavior = ?
                    WHERE id = ?
                    """,
                    (
                        1 if verification_passed else 0,
                        verification_checks_json,
                        expected_behavior,
                        int(row["id"]),
                    ),
                )

    def verification_summary(self, repo_root: str) -> tuple[int, int]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN verification_passed = 1 THEN 1 ELSE 0 END) AS passed
                FROM decision_traces
                WHERE repo_root = ? AND stage = 'apply' AND verification_passed IS NOT NULL
                """,
                (repo_root,),
            ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["total"] or 0), int(row["passed"] or 0))

    def session_verification_status(self, repo_root: str, session_id: str) -> bool | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT verification_passed
                FROM decision_traces
                WHERE repo_root = ? AND session_id = ? AND stage = 'apply' AND verification_passed IS NOT NULL
                ORDER BY created_at DESC, id DESC
                """,
                (repo_root, session_id),
            ).fetchall()
        if not rows:
            return None
        return all(int(row["verification_passed"]) == 1 for row in rows)

    def checkin_usefulness_summary(
        self,
        repo_root: str,
        *,
        quick_approve_ms: int = 5000,
    ) -> list["CheckInUsefulnessSummary"]:
        from ..trust_db import CheckInUsefulnessSummary
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    check_in_initiator,
                    user_decision,
                    response_time_ms,
                    user_feedback_text
                FROM decision_traces
                WHERE repo_root = ?
                  AND check_in_initiator IS NOT NULL
                  AND check_in_initiator != ''
                """,
                (repo_root,),
            ).fetchall()

        by_initiator: dict[str, dict[str, int]] = {}
        for row in rows:
            initiator = str(row["check_in_initiator"])
            counts = by_initiator.setdefault(
                initiator,
                {"total": 0, "useful": 0, "wasted": 0},
            )
            counts["total"] += 1
            decision = str(row["user_decision"] or "")
            response_ms = row["response_time_ms"]
            feedback = (row["user_feedback_text"] or "").strip()
            has_feedback = bool(feedback)
            thoughtful_review = response_ms is not None and int(response_ms) > max(quick_approve_ms, 0)

            useful = (
                decision in {"deny", "revise"}
                or has_feedback
                or thoughtful_review
            )
            if useful:
                counts["useful"] += 1
            else:
                counts["wasted"] += 1

        return [
            CheckInUsefulnessSummary(
                initiator=initiator,
                total=values["total"],
                useful=values["useful"],
                wasted=values["wasted"],
            )
            for initiator, values in sorted(by_initiator.items())
        ]

    def access_stats(self, repo_root: str, limit: int = 200) -> "AccessStats":
        from ..trust_db import AccessStats
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT decision_type, touched_files_json
                FROM decisions
                WHERE repo_root = ? AND decision_type IN ('read', 'apply')
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (repo_root, max(limit, 1)),
            ).fetchall()
        read_actions = 0
        write_actions = 0
        write_file_counts: list[int] = []
        multi_file_write_actions = 0
        for row in rows:
            decision_type = str(row["decision_type"])
            touched_raw = row["touched_files_json"]
            touched_count = 0
            if touched_raw:
                try:
                    touched = json.loads(touched_raw)
                    if isinstance(touched, list):
                        touched_count = len(touched)
                except Exception:
                    touched_count = 0
            if decision_type == "read":
                read_actions += 1
            elif decision_type == "apply":
                write_actions += 1
                write_file_counts.append(max(touched_count, 0))
                if touched_count > 1:
                    multi_file_write_actions += 1
        avg_files = None
        if write_file_counts:
            avg_files = sum(write_file_counts) / len(write_file_counts)
        return AccessStats(
            read_actions=read_actions,
            write_actions=write_actions,
            avg_files_per_write=avg_files,
            multi_file_write_actions=multi_file_write_actions,
        )

    def checkin_calibration(self, repo_root: str) -> list["CheckInCalibration"]:
        """Aggregate check-in outcomes by initiator/stage for calibration analysis."""
        from ..trust_db import CheckInCalibration
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    check_in_initiator AS initiator,
                    stage,
                    COUNT(*) AS total,
                    SUM(CASE WHEN user_decision IN ('approve', 'approve_and_remember') THEN 1 ELSE 0 END) AS approvals,
                    SUM(CASE WHEN user_decision = 'deny' THEN 1 ELSE 0 END) AS denials,
                    AVG(response_time_ms) AS avg_response_ms
                FROM decision_traces
                WHERE repo_root = ?
                  AND check_in_initiator IS NOT NULL
                  AND check_in_initiator != ''
                GROUP BY check_in_initiator, stage
                ORDER BY check_in_initiator, stage
                """,
                (repo_root,),
            ).fetchall()
        stats: list = []
        for row in rows:
            total = int(row["total"] or 0)
            approvals = int(row["approvals"] or 0)
            denials = int(row["denials"] or 0)
            approval_rate = (approvals / total) if total > 0 else 0.0
            avg_response_ms = float(row["avg_response_ms"]) if row["avg_response_ms"] is not None else None
            stats.append(
                CheckInCalibration(
                    initiator=str(row["initiator"]),
                    stage=str(row["stage"]),
                    total=total,
                    approvals=approvals,
                    denials=denials,
                    approval_rate=approval_rate,
                    avg_response_ms=avg_response_ms,
                )
            )
        return stats

    def model_checkin_calibration(self, repo_root: str) -> tuple[int, float | None]:
        rows = self.checkin_calibration(repo_root)
        model_rows = [row for row in rows if row.initiator == "model_proactive"]
        if not model_rows:
            return 0, None
        total = sum(row.total for row in model_rows)
        approvals = sum(row.approvals for row in model_rows)
        if total <= 0:
            return 0, None
        return total, approvals / total

    def trust_summary(self, repo_root: str) -> "TrustSummary":
        from ..trust_db import TrustSummary
        approve_values = {
            "approve",
            "approve_and_remember",
            "auto_approve",
            "auto_approve_flag",
            "auto_approve_lease",
            "auto_approve_read_lease",
        }
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT file_path, user_decision
                FROM decision_traces
                WHERE repo_root = ? AND stage = 'apply'
                """,
                (repo_root,),
            ).fetchall()
            pattern_rows = conn.execute(
                """
                SELECT change_type, user_decision
                FROM decision_traces
                WHERE repo_root = ? AND stage = 'apply' AND change_type IS NOT NULL
                """,
                (repo_root,),
            ).fetchall()

        by_file: dict[str, dict[str, int]] = {}
        for row in rows:
            file_path = row["file_path"]
            stats = by_file.setdefault(file_path, {"approvals": 0, "denials": 0})
            if row["user_decision"] in approve_values:
                stats["approvals"] += 1
            if row["user_decision"] == "deny":
                stats["denials"] += 1

        high_files = [path for path, stats in by_file.items() if stats["approvals"] >= 3 and stats["denials"] == 0]
        low_files = [path for path, stats in by_file.items() if stats["denials"] >= 2 or (stats["denials"] >= 1 and stats["approvals"] == 0)]

        def _area(path: str) -> str:
            parts = Path(path).parts
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
            if parts:
                return parts[0]
            return path

        high_areas = list(dict.fromkeys(_area(path) for path in sorted(high_files)))[:6]
        low_areas = list(dict.fromkeys(_area(path) for path in sorted(low_files)))[:6]

        corrected_counter: dict[str, int] = {}
        for row in pattern_rows:
            if row["user_decision"] != "deny":
                continue
            pattern = str(row["change_type"])
            if ":" in pattern:
                pattern = pattern.split(":", 1)[1]
            corrected_counter[pattern] = corrected_counter.get(pattern, 0) + 1
        corrected_patterns = [
            item[0]
            for item in sorted(corrected_counter.items(), key=lambda kv: (-kv[1], kv[0]))
            if item[0]
        ][:6]
        return TrustSummary(
            high_trust_areas=high_areas,
            low_trust_areas=low_areas,
            corrected_patterns=corrected_patterns,
        )
