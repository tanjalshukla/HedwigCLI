from __future__ import annotations

### This is mostly an AI-Generate file based off the DB schema I designed

### TODO: modularize code in this file, clean up boilerplate

# central persistence layer for governance state.
# stores leases, decision traces, constraints, guidelines, and calibration data.
# schema migrations are additive (new columns only) to avoid breaking existing dbs.

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterator, Iterable


# --- data classes returned by query methods ---

@dataclass(frozen=True)
class Lease:
    file_path: str
    expires_at: int | None
    lease_type: str


@dataclass(frozen=True)
class PolicyHistory:
    approvals: int
    denials: int
    # rubber-stamp approvals count as 0.5x (see spec §10 approval quality weighting)
    effective_approvals: float
    rubber_stamp_approvals: int
    avg_response_ms: float | None
    avg_edit_distance: float | None


@dataclass(frozen=True)
class HardConstraint:
    path_pattern: str
    constraint_type: str
    source: str
    overridable: bool


@dataclass(frozen=True)
class BehavioralGuideline:
    guideline: str
    source: str


# injected into the system prompt as vague area names (no numeric scores)
# so the model can reason about uncertainty without gaming thresholds
@dataclass(frozen=True)
class TrustSummary:
    high_trust_areas: list[str]
    low_trust_areas: list[str]
    corrected_patterns: list[str]


@dataclass(frozen=True)
class CheckInCalibration:
    initiator: str
    stage: str
    total: int
    approvals: int
    denials: int
    approval_rate: float
    avg_response_ms: float | None


@dataclass(frozen=True)
class PlanRevisionSummary:
    total: int
    approved: int
    revisions_requested: int
    denied: int


@dataclass(frozen=True)
class CheckInUsefulnessSummary:
    initiator: str
    total: int
    useful: int
    wasted: int

    @property
    def useful_rate(self) -> float:
        return (self.useful / self.total) if self.total > 0 else 0.0


@dataclass(frozen=True)
class GuidelineCandidate:
    guideline: str
    count: int


class TrustDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leases (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    source TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS read_leases (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    source TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    task TEXT NOT NULL,
                    decision_type TEXT NOT NULL,
                    approved INTEGER NOT NULL,
                    remembered INTEGER NOT NULL,
                    planned_files_json TEXT NOT NULL,
                    touched_files_json TEXT,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_traces (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    change_type TEXT,
                    diff_size INTEGER,
                    blast_radius INTEGER,
                    existing_lease INTEGER NOT NULL,
                    lease_type TEXT,
                    prior_approvals INTEGER NOT NULL,
                    prior_denials INTEGER NOT NULL,
                    policy_action TEXT NOT NULL,
                    policy_score REAL NOT NULL,
                    policy_reasons_json TEXT,
                    user_decision TEXT NOT NULL,
                    response_time_ms INTEGER,
                    review_duration_seconds REAL,
                    rubber_stamp INTEGER,
                    edit_distance REAL,
                    user_feedback_text TEXT,
                    verification_passed INTEGER,
                    verification_checks_json TEXT,
                    expected_behavior TEXT,
                    model_confidence_self_report REAL,
                    model_assumptions_json TEXT,
                    check_in_initiator TEXT,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plan_revisions (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    revision_round INTEGER NOT NULL,
                    plan_hash TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    developer_feedback TEXT,
                    approved INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hard_constraints (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    path_pattern TEXT NOT NULL,
                    constraint_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    overridable INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS behavioral_guidelines (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    guideline TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leases_repo_file ON leases (repo_root, file_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leases_expires ON leases (expires_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_read_leases_repo_file ON read_leases (repo_root, file_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_read_leases_expires ON read_leases (expires_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_repo_stage_file ON decision_traces (repo_root, stage, file_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_repo_session ON decision_traces (repo_root, session_id)"
            )
            migrations = [
                ("policy_reasons_json", "TEXT"),
                ("user_feedback_text", "TEXT"),
                ("review_duration_seconds", "REAL"),
                ("rubber_stamp", "INTEGER"),
                ("check_in_initiator", "TEXT"),
                ("verification_passed", "INTEGER"),
                ("verification_checks_json", "TEXT"),
                ("expected_behavior", "TEXT"),
                ("model_confidence_self_report", "REAL"),
                ("model_assumptions_json", "TEXT"),
            ]
            for column, definition in migrations:
                self._ensure_column(
                    conn,
                    table="decision_traces",
                    column=column,
                    definition=definition,
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_traces_repo_initiator ON decision_traces (repo_root, check_in_initiator)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_plan_revisions_repo_session ON plan_revisions (repo_root, session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_constraints_repo_pattern ON hard_constraints (repo_root, path_pattern)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_constraints_repo_source ON hard_constraints (repo_root, source)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_guidelines_repo_source ON behavioral_guidelines (repo_root, source)"
            )

    def _ensure_column(self, conn: sqlite3.Connection, *, table: str, column: str, definition: str) -> None:
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in columns):
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def active_leases(self, repo_root: str, files: Iterable[str]) -> dict[str, Lease]:
        files_list = list(files)
        if not files_list:
            return {}
        now = int(time.time())
        placeholders = ",".join("?" for _ in files_list)
        query = (
            "SELECT file_path, expires_at FROM leases "
            "WHERE repo_root = ? AND file_path IN ({}) AND (expires_at IS NULL OR expires_at > ?)"
        ).format(placeholders)
        params = [repo_root, *files_list, now]
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {
            row["file_path"]: Lease(row["file_path"], row["expires_at"], "write") for row in rows
        }

    def active_read_leases(self, repo_root: str, files: Iterable[str]) -> dict[str, Lease]:
        files_list = list(files)
        if not files_list:
            return {}
        now = int(time.time())
        placeholders = ",".join("?" for _ in files_list)
        query = (
            "SELECT file_path, expires_at FROM read_leases "
            "WHERE repo_root = ? AND file_path IN ({}) AND (expires_at IS NULL OR expires_at > ?)"
        ).format(placeholders)
        params = [repo_root, *files_list, now]
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return {
            row["file_path"]: Lease(row["file_path"], row["expires_at"], "read") for row in rows
        }

    def list_active_leases(self, repo_root: str) -> list[Lease]:
        now = int(time.time())
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT file_path, expires_at FROM leases WHERE repo_root = ? AND (expires_at IS NULL OR expires_at > ?) ORDER BY file_path",
                (repo_root, now),
            ).fetchall()
            read_rows = conn.execute(
                "SELECT file_path, expires_at FROM read_leases WHERE repo_root = ? AND (expires_at IS NULL OR expires_at > ?) ORDER BY file_path",
                (repo_root, now),
            ).fetchall()
        leases = [Lease(row["file_path"], row["expires_at"], "write") for row in rows]
        leases.extend([Lease(row["file_path"], row["expires_at"], "read") for row in read_rows])
        return leases

    def add_leases(self, repo_root: str, files: Iterable[str], ttl_hours: int, source: str) -> None:
        files_list = list(dict.fromkeys(files))
        if not files_list:
            return
        now = int(time.time())
        expires_at = now + ttl_hours * 3600
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO leases (repo_root, file_path, created_at, expires_at, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(repo_root, file_path, now, expires_at, source) for file_path in files_list],
            )

    def add_permanent_leases(self, repo_root: str, files: Iterable[str], source: str) -> None:
        files_list = list(dict.fromkeys(files))
        if not files_list:
            return
        now = int(time.time())
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO leases (repo_root, file_path, created_at, expires_at, source)
                VALUES (?, ?, ?, NULL, ?)
                """,
                [(repo_root, file_path, now, source) for file_path in files_list],
            )

    def add_permanent_read_leases(self, repo_root: str, files: Iterable[str], source: str) -> None:
        files_list = list(dict.fromkeys(files))
        if not files_list:
            return
        now = int(time.time())
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO read_leases (repo_root, file_path, created_at, expires_at, source)
                VALUES (?, ?, ?, NULL, ?)
                """,
                [(repo_root, file_path, now, source) for file_path in files_list],
            )

    def revoke(
        self,
        repo_root: str,
        file_path: str | None = None,
        reset_counts: bool = False,
    ) -> tuple[int, int]:
        removed_leases = 0
        removed_decisions = 0
        with self._connect() as conn:
            if file_path:
                result = conn.execute(
                    "DELETE FROM leases WHERE repo_root = ? AND file_path = ?",
                    (repo_root, file_path),
                )
                removed_leases += result.rowcount
                result = conn.execute(
                    "DELETE FROM read_leases WHERE repo_root = ? AND file_path = ?",
                    (repo_root, file_path),
                )
                removed_leases += result.rowcount
                if reset_counts:
                    rows = conn.execute(
                        """
                        SELECT id, touched_files_json FROM decisions
                        WHERE repo_root = ? AND touched_files_json IS NOT NULL
                        """,
                        (repo_root,),
                    ).fetchall()
                    for row in rows:
                        try:
                            touched = json.loads(row["touched_files_json"])
                        except Exception:
                            continue
                        if file_path in touched:
                            conn.execute("DELETE FROM decisions WHERE id = ?", (row["id"],))
                            removed_decisions += 1
            else:
                result = conn.execute("DELETE FROM leases WHERE repo_root = ?", (repo_root,))
                removed_leases += result.rowcount
                result = conn.execute("DELETE FROM read_leases WHERE repo_root = ?", (repo_root,))
                removed_leases += result.rowcount
                if reset_counts:
                    result = conn.execute("DELETE FROM decisions WHERE repo_root = ?", (repo_root,))
                    removed_decisions = result.rowcount
        return removed_leases, removed_decisions

    def replace_constraints(
        self,
        repo_root: str,
        source: str,
        constraints: Iterable[HardConstraint],
    ) -> int:
        now = int(time.time())
        unique: dict[tuple[str, str, str], HardConstraint] = {}
        for constraint in constraints:
            key = (constraint.path_pattern, constraint.constraint_type, constraint.source)
            unique[key] = constraint

        with self._connect() as conn:
            conn.execute(
                "DELETE FROM hard_constraints WHERE repo_root = ? AND source = ?",
                (repo_root, source),
            )
            if not unique:
                return 0
            conn.executemany(
                """
                INSERT INTO hard_constraints (
                    repo_root, path_pattern, constraint_type, source, overridable, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        repo_root,
                        item.path_pattern,
                        item.constraint_type,
                        item.source,
                        1 if item.overridable else 0,
                        now,
                    )
                    for item in unique.values()
                ],
            )
        return len(unique)

    def list_constraints(self, repo_root: str) -> list[HardConstraint]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT path_pattern, constraint_type, source, overridable
                FROM hard_constraints
                WHERE repo_root = ?
                ORDER BY path_pattern, constraint_type
                """,
                (repo_root,),
            ).fetchall()
        return [
            HardConstraint(
                path_pattern=row["path_pattern"],
                constraint_type=row["constraint_type"],
                source=row["source"],
                overridable=bool(row["overridable"]),
            )
            for row in rows
        ]

    def matching_constraints(self, repo_root: str, file_path: str) -> list[HardConstraint]:
        all_constraints = self.list_constraints(repo_root)
        return [constraint for constraint in all_constraints if fnmatch(file_path, constraint.path_pattern)]

    def strongest_constraint(self, repo_root: str, file_path: str) -> HardConstraint | None:
        priority = {"always_deny": 3, "always_check_in": 2, "always_allow": 1}
        matched = self.matching_constraints(repo_root, file_path)
        if not matched:
            return None
        return max(matched, key=lambda item: priority.get(item.constraint_type, 0))

    def delete_constraints(
        self,
        repo_root: str,
        *,
        source: str | None = None,
        path_pattern: str | None = None,
    ) -> int:
        where = ["repo_root = ?"]
        params: list[str] = [repo_root]
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if path_pattern is not None:
            where.append("path_pattern = ?")
            params.append(path_pattern)
        query = "DELETE FROM hard_constraints WHERE " + " AND ".join(where)
        with self._connect() as conn:
            result = conn.execute(query, params)
        return int(result.rowcount)

    def replace_behavioral_guidelines(
        self,
        repo_root: str,
        source: str,
        guidelines: Iterable[str],
    ) -> int:
        now = int(time.time())
        unique = [item.strip() for item in dict.fromkeys(guidelines) if item.strip()]
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM behavioral_guidelines WHERE repo_root = ? AND source = ?",
                (repo_root, source),
            )
            if not unique:
                return 0
            conn.executemany(
                """
                INSERT INTO behavioral_guidelines (repo_root, guideline, source, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [(repo_root, item, source, now) for item in unique],
            )
        return len(unique)

    def add_behavioral_guidelines(
        self,
        repo_root: str,
        source: str,
        guidelines: Iterable[str],
    ) -> int:
        """Append new guidelines without deleting existing ones."""
        items = [item.strip() for item in dict.fromkeys(guidelines) if item.strip()]
        if not items:
            return 0
        now = int(time.time())
        inserted = 0
        with self._connect() as conn:
            for guideline in items:
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM behavioral_guidelines
                    WHERE repo_root = ? AND guideline = ?
                    LIMIT 1
                    """,
                    (repo_root, guideline),
                ).fetchone()
                if existing is not None:
                    continue
                conn.execute(
                    """
                    INSERT INTO behavioral_guidelines (repo_root, guideline, source, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (repo_root, guideline, source, now),
                )
                inserted += 1
        return inserted

    def list_behavioral_guidelines(self, repo_root: str) -> list[BehavioralGuideline]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT guideline, source
                FROM behavioral_guidelines
                WHERE repo_root = ?
                ORDER BY source, id
                """,
                (repo_root,),
            ).fetchall()
        return [BehavioralGuideline(guideline=row["guideline"], source=row["source"]) for row in rows]

    def delete_behavioral_guidelines(
        self,
        repo_root: str,
        *,
        source: str | None = None,
    ) -> int:
        where = ["repo_root = ?"]
        params: list[str] = [repo_root]
        if source is not None:
            where.append("source = ?")
            params.append(source)
        query = "DELETE FROM behavioral_guidelines WHERE " + " AND ".join(where)
        with self._connect() as conn:
            result = conn.execute(query, params)
        return int(result.rowcount)

    def guideline_candidates(
        self,
        repo_root: str,
        *,
        min_count: int = 2,
        max_items: int = 8,
    ) -> list[GuidelineCandidate]:
        """Suggest guidelines from repeated developer feedback text in traces."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_feedback_text
                FROM decision_traces
                WHERE repo_root = ?
                  AND user_feedback_text IS NOT NULL
                  AND TRIM(user_feedback_text) != ''
                ORDER BY created_at DESC, id DESC
                LIMIT 500
                """,
                (repo_root,),
            ).fetchall()

        counts: dict[str, int] = {}
        canonical: dict[str, str] = {}
        existing = {
            item.guideline.lower()
            for item in self.list_behavioral_guidelines(repo_root)
        }
        for row in rows:
            raw = " ".join(str(row["user_feedback_text"]).split()).strip()
            if not raw:
                continue
            key = raw.lower()
            if key in existing:
                continue
            counts[key] = counts.get(key, 0) + 1
            canonical.setdefault(key, raw)

        suggestions = [
            GuidelineCandidate(guideline=canonical[key], count=value)
            for key, value in counts.items()
            if value >= max(min_count, 1)
        ]
        suggestions.sort(key=lambda item: (-item.count, item.guideline))
        return suggestions[: max(max_items, 1)]

    def trust_summary(self, repo_root: str) -> TrustSummary:
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
                    check_in_initiator, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    now,
                ),
            )

    def policy_history(self, repo_root: str, file_path: str, stage: str) -> PolicyHistory:
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
                    check_in_initiator, created_at
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
                    id, stage, action_type, file_path, change_type, policy_action, policy_score,
                    user_decision, response_time_ms, review_duration_seconds, rubber_stamp,
                    user_feedback_text, check_in_initiator, created_at
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
    ) -> None:
        now = int(time.time())
        reasons_json = json.dumps(list(reasons))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plan_revisions (
                    repo_root, session_id, task, revision_round, plan_hash, intent_json,
                    reasons_json, developer_feedback, approved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    now,
                ),
            )

    def plan_revision_summary(self, repo_root: str) -> PlanRevisionSummary:
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
        placeholders = ",".join("?" for _ in files_list)
        query = (
            "UPDATE decision_traces "
            "SET verification_passed = ?, verification_checks_json = ?, expected_behavior = ? "
            "WHERE repo_root = ? AND session_id = ? AND stage = 'apply' AND file_path IN ({})"
        ).format(placeholders)
        params = [
            1 if verification_passed else 0,
            verification_checks_json,
            expected_behavior,
            repo_root,
            session_id,
            *files_list,
        ]
        with self._connect() as conn:
            conn.execute(query, params)

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

    def checkin_usefulness_summary(
        self,
        repo_root: str,
        *,
        quick_approve_ms: int = 5000,
    ) -> list[CheckInUsefulnessSummary]:
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

    def recent_feedback_snippets(self, repo_root: str, limit: int = 4) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_feedback_text
                FROM decision_traces
                WHERE repo_root = ?
                  AND user_feedback_text IS NOT NULL
                  AND TRIM(user_feedback_text) != ''
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """,
                (repo_root,),
            ).fetchall()

        snippets: list[str] = []
        seen: set[str] = set()
        for row in rows:
            text = " ".join(str(row["user_feedback_text"]).split()).strip()
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            snippets.append(text[:220])
            if len(snippets) >= max(limit, 1):
                break
        return snippets

    def checkin_calibration(self, repo_root: str) -> list[CheckInCalibration]:
        """Aggregate check-in outcomes by initiator/stage for calibration analysis."""
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
        stats: list[CheckInCalibration] = []
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

    def approved_apply_counts(self, repo_root: str, files: Iterable[str]) -> dict[str, int]:
        target = set(files)
        if not target:
            return {}
        counts = {path: 0 for path in target}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT touched_files_json FROM decisions
                WHERE repo_root = ? AND decision_type = 'apply' AND approved = 1 AND touched_files_json IS NOT NULL
                """,
                (repo_root,),
            ).fetchall()
        for row in rows:
            try:
                touched = json.loads(row["touched_files_json"])
            except Exception:
                continue
            for path in touched:
                if path in counts:
                    counts[path] += 1
        return counts

    # Read approvals are permanent once granted; no counters needed.
