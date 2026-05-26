from __future__ import annotations

"""Central persistence layer — all SQLite state lives here.

Nine tables (see _ensure_schema for DDL):
  decision_traces        — immutable record of every governed action
  leases / read_leases   — temporary trust grants
  hard_constraints       — non-negotiable path rules
  behavioral_guidelines  — soft prompt-level guidance
  logic_notes            — semantic summaries injected into future prompts
  autonomy_preferences   — coarse AutonomyPreferences per repo
  confirmed_preferences  — developer-confirmed Preference objects (5-dim)
  policy_models          — serialized PolicyClassifier blobs
  hypothesis_candidates  — hypothesis bank (pending/ready/confirmed/rejected/declined)

Schema migrations are additive (ALTER TABLE ADD COLUMN only) so existing
databases from prior versions remain readable. The `_ensure_schema` method
runs on every connect.

TrustDB delegates to five focused mixin stores:
  LeaseStoreMixin  — leases and approved-apply counts
  RuleStoreMixin   — constraints, guidelines, logic notes, retrieval
  TraceStoreMixin  — decision traces, plan revisions, calibration analytics
  PrefStoreMixin   — autonomy preferences and hypothesis candidates
  ModelStoreMixin  — PolicyClassifier persistence and snapshots
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .store.lease_store import LeaseStoreMixin
from .store.rule_store import RuleStoreMixin
from .store.trace_store import TraceStoreMixin
from .store.pref_store import PrefStoreMixin
from .store.model_store import ModelStoreMixin


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
    source: str
    overridable: bool
    constraint_type: str | None = None
    read_policy: str | None = None
    write_policy: str | None = None

    def __post_init__(self) -> None:
        shared = self.constraint_type or self.read_policy or self.write_policy or "always_check_in"
        read_policy = self.read_policy or shared
        write_policy = self.write_policy or shared
        object.__setattr__(self, "read_policy", read_policy)
        object.__setattr__(self, "write_policy", write_policy)
        if read_policy == write_policy:
            normalized = read_policy
        else:
            normalized = f"read:{read_policy}, write:{write_policy}"
        object.__setattr__(self, "constraint_type", normalized)

    def policy_for(self, access_type: str) -> str:
        return self.read_policy if access_type == "read" else self.write_policy

    @classmethod
    def for_both(
        cls,
        *,
        path_pattern: str,
        constraint_type: str,
        source: str,
        overridable: bool,
    ) -> "HardConstraint":
        return cls(
            path_pattern=path_pattern,
            source=source,
            overridable=overridable,
            constraint_type=constraint_type,
        )


@dataclass(frozen=True)
class BehavioralGuideline:
    guideline: str
    source: str


@dataclass(frozen=True)
class LogicNote:
    note: str
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


@dataclass(frozen=True)
class AccessStats:
    read_actions: int
    write_actions: int
    avg_files_per_write: float | None
    multi_file_write_actions: int


@dataclass(frozen=True)
class ModelConfidenceStats:
    average: float | None
    samples: int


class TrustDB(
    LeaseStoreMixin,
    RuleStoreMixin,
    TraceStoreMixin,
    PrefStoreMixin,
    ModelStoreMixin,
):
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Retry up to 5 s on write-lock contention. Required because the LLM
        # hypothesis generator runs in a daemon thread and writes concurrently
        # with the main thread. Without a timeout, concurrent writers raise
        # OperationalError: database is locked immediately.
        conn.execute("PRAGMA busy_timeout=5000")
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
            # WAL mode allows concurrent readers while a write is in progress,
            # reducing lock contention with the daemon-thread LLM generator.
            conn.execute("PRAGMA journal_mode=WAL")
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
            self._dedupe_lease_rows(conn, table="leases")
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
            self._dedupe_lease_rows(conn, table="read_leases")
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
                    participant_id TEXT,
                    study_run_id TEXT,
                    study_task_id TEXT,
                    autonomy_mode TEXT,
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
                    participant_id TEXT,
                    study_run_id TEXT,
                    study_task_id TEXT,
                    autonomy_mode TEXT,
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
                    read_policy TEXT,
                    write_policy TEXT,
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
                """
                CREATE TABLE IF NOT EXISTS logic_notes (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    note TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    change_types_json TEXT,
                    source TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS autonomy_preferences (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL UNIQUE,
                    preferences_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS confirmed_preferences (
                    id INTEGER PRIMARY KEY,
                    repo_root TEXT NOT NULL,
                    session_id TEXT,
                    preference_json TEXT NOT NULL,
                    driver TEXT,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_confirmed_prefs_repo_session "
                "ON confirmed_preferences (repo_root, session_id)"
            )
            conn.execute("DROP INDEX IF EXISTS idx_leases_repo_file")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_repo_file ON leases (repo_root, file_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leases_expires ON leases (expires_at)"
            )
            conn.execute("DROP INDEX IF EXISTS idx_read_leases_repo_file")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_read_leases_repo_file ON read_leases (repo_root, file_path)"
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
            # Hypothesis candidate bank — generated hypotheses tracked before surfacing.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hypothesis_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_root TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    driver TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'rule_based',
                    prompt TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    preference_json TEXT NOT NULL,
                    evidence_for INTEGER NOT NULL DEFAULT 0,
                    evidence_against INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_hypothesis_candidates_repo_session "
                "ON hypothesis_candidates (repo_root, session_id, status)"
            )
            # Per-candidate evidence floor. NULL means use the global
            # MIN_EVIDENCE; the LLM noticer may set this higher (never lower)
            # for high-stakes hypotheses (e.g. anything proposing auto_apply
            # or touching auth). Floor never moves below MIN_EVIDENCE.
            self._ensure_column(
                conn,
                table="hypothesis_candidates",
                column="min_evidence",
                definition="INTEGER",
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
                ("participant_id", "TEXT"),
                ("study_run_id", "TEXT"),
                ("study_task_id", "TEXT"),
                ("autonomy_mode", "TEXT"),
                # Pushback category (per SWE-chat-grounded PushbackType enum):
                # correction / rejection / failure_report / non_pushback.
                ("pushback_type", "TEXT"),
                # Scorer uncertainty: |score - 0.5|. Distance from 50-50.
                # Low values = uncertain decisions; supports uncertainty-triggered
                # check-in analysis without re-deriving from policy_score.
                ("scorer_uncertainty", "REAL"),
                # Turn purpose — orthogonal to pushback_type. What this turn
                # is *for* (context_provision, structured_spec_input, etc.),
                # regardless of whether it technically counts as pushback.
                ("turn_purpose", "TEXT"),
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
            plan_migrations = [
                ("participant_id", "TEXT"),
                ("study_run_id", "TEXT"),
                ("study_task_id", "TEXT"),
                ("autonomy_mode", "TEXT"),
            ]
            for column, definition in plan_migrations:
                self._ensure_column(
                    conn,
                    table="plan_revisions",
                    column=column,
                    definition=definition,
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_constraints_repo_pattern ON hard_constraints (repo_root, path_pattern)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_constraints_repo_source ON hard_constraints (repo_root, source)"
            )
            constraint_migrations = [
                ("read_policy", "TEXT"),
                ("write_policy", "TEXT"),
            ]
            for column, definition in constraint_migrations:
                self._ensure_column(
                    conn,
                    table="hard_constraints",
                    column=column,
                    definition=definition,
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_guidelines_repo_source ON behavioral_guidelines (repo_root, source)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logic_notes_repo_source ON logic_notes (repo_root, source)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_autonomy_prefs_repo ON autonomy_preferences (repo_root)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS policy_models (
                    repo_root TEXT PRIMARY KEY,
                    model_blob BLOB NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            # Snapshots of prior model_blob values, keyed by repo + timestamp.
            # Used as a safety net — `hw observe rollback` restores the most
            # recent snapshot. Ring-bounded to _SNAPSHOT_RETENTION rows per
            # repo; older entries are pruned on save.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS policy_model_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_root TEXT NOT NULL,
                    model_blob BLOB NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_policy_model_snapshots_repo
                ON policy_model_snapshots(repo_root, created_at DESC)
                """
            )

    def _ensure_column(self, conn: sqlite3.Connection, *, table: str, column: str, definition: str) -> None:
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in columns):
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
