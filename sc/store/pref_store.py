from __future__ import annotations

"""PrefStoreMixin — autonomy preferences and confirmed preference hypotheses.

Methods: autonomy_preferences, merge_autonomy_preferences, revoke_autonomy_preference,
_save_autonomy_preferences, delete_autonomy_preferences, save_confirmed_preference,
session_has_confirmed_hypothesis, confirmed_preferences_for_repo,
confirmed_preferences_for_session, add_hypothesis_candidate,
get_pending_hypothesis_candidates, update_hypothesis_evidence,
set_hypothesis_status, candidate_driver_exists.
"""

import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..autonomy import AutonomyPreferences


class PrefStoreMixin:
    # _connect and autonomy imports are provided by TrustDB.

    def autonomy_preferences(self, repo_root: str) -> "AutonomyPreferences":
        from ..autonomy import AutonomyPreferences
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT preferences_json
                FROM autonomy_preferences
                WHERE repo_root = ?
                """,
                (repo_root,),
            ).fetchone()
        if row is None:
            return AutonomyPreferences()
        return AutonomyPreferences.from_json(row["preferences_json"])

    def merge_autonomy_preferences(
        self,
        repo_root: str,
        inferred: "AutonomyPreferences",
    ) -> list[str]:
        from ..autonomy import merge_preferences
        current = self.autonomy_preferences(repo_root)
        updated, learned = merge_preferences(current, inferred)
        if updated == current:
            return []
        self._save_autonomy_preferences(repo_root, updated)
        return learned

    def revoke_autonomy_preference(
        self,
        repo_root: str,
        *,
        topics: tuple[str, ...] = (),
        paths: tuple[str, ...] = (),
        prefer_fewer_checkins: bool = False,
        skip_low_risk_plan_checkpoint: bool = False,
    ) -> list[str]:
        """Subtract specific preferences from the current stored state.

        Returns human-readable descriptions of what was revoked, or an
        empty list if nothing changed.
        """
        from ..autonomy import revoke_preferences
        current = self.autonomy_preferences(repo_root)
        updated, revoked = revoke_preferences(
            current,
            topics=topics,
            paths=paths,
            prefer_fewer_checkins=prefer_fewer_checkins,
            skip_low_risk_plan_checkpoint=skip_low_risk_plan_checkpoint,
        )
        if updated == current:
            return []
        self._save_autonomy_preferences(repo_root, updated)
        return revoked

    def _save_autonomy_preferences(self, repo_root: str, preferences: "AutonomyPreferences") -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO autonomy_preferences (repo_root, preferences_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(repo_root) DO UPDATE SET
                    preferences_json = excluded.preferences_json,
                    updated_at = excluded.updated_at
                """,
                (repo_root, preferences.to_json(), now),
            )

    def delete_autonomy_preferences(self, repo_root: str) -> int:
        with self._connect() as conn:
            result = conn.execute(
                """
                DELETE FROM autonomy_preferences
                WHERE repo_root = ?
                """,
                (repo_root,),
            )
        return int(result.rowcount)

    def save_confirmed_preference(
        self,
        *,
        repo_root: str,
        session_id: str | None,
        preference_json: str,
        driver: str | None,
    ) -> None:
        """Persist a developer-confirmed Preference as JSON."""
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO confirmed_preferences
                    (repo_root, session_id, preference_json, driver, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (repo_root, session_id, preference_json, driver, now),
            )

    def session_has_confirmed_hypothesis(
        self, repo_root: str, session_id: str, driver: str | None = None
    ) -> bool:
        """Has this driver already been surfaced in this session?

        With driver=None (legacy): returns True if ANY hypothesis was surfaced.
        With driver specified: returns True only if that specific driver was
        already asked. This allows multiple different hypotheses per session
        while preventing the same pattern from being asked twice.
        """
        with self._connect() as conn:
            if driver is not None:
                row = conn.execute(
                    "SELECT 1 FROM confirmed_preferences "
                    "WHERE repo_root = ? AND session_id = ? AND driver = ? LIMIT 1",
                    (repo_root, session_id, driver),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM confirmed_preferences "
                    "WHERE repo_root = ? AND session_id = ? LIMIT 1",
                    (repo_root, session_id),
                ).fetchone()
        return row is not None

    def confirmed_preferences_for_repo(
        self, repo_root: str, *, limit: int = 20
    ) -> list[sqlite3.Row]:
        """All accepted confirmed preferences for this repo, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT preference_json, driver, session_id, created_at "
                "FROM confirmed_preferences "
                "WHERE repo_root = ? "
                "ORDER BY created_at DESC "
                "LIMIT ?",
                (repo_root, limit),
            ).fetchall()
        return rows

    def confirmed_preferences_for_session(
        self, repo_root: str, session_id: str
    ) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT preference_json, driver, created_at "
                "FROM confirmed_preferences "
                "WHERE repo_root = ? AND session_id = ? "
                "ORDER BY created_at ASC",
                (repo_root, session_id),
            ).fetchall()
        return rows

    def add_hypothesis_candidate(
        self,
        *,
        repo_root: str,
        session_id: str,
        driver: str,
        source: str,
        prompt: str,
        rationale: str,
        preference_json: str,
        min_evidence: int | None = None,
    ) -> int:
        """Insert a new pending candidate. Returns its id.

        ``min_evidence`` may raise the surfacing floor for this candidate
        (never lowers it below the global MIN_EVIDENCE). NULL = use global.
        """
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO hypothesis_candidates
                    (repo_root, session_id, driver, source, prompt, rationale,
                     preference_json, evidence_for, evidence_against, status,
                     created_at, min_evidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'pending', ?, ?)
                """,
                (repo_root, session_id, driver, source, prompt, rationale,
                 preference_json, now, min_evidence),
            )
        return int(cur.lastrowid)

    def get_pending_hypothesis_candidates(
        self, repo_root: str, session_id: str
    ) -> list[sqlite3.Row]:
        # Scoped to repo, not session: hypotheses are repo-level (so seeded
        # candidates from the seed_demo session, and candidates from prior
        # live sessions, can both accumulate evidence in the current session).
        del session_id
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, driver, source, prompt, rationale, preference_json,
                       evidence_for, evidence_against, status, min_evidence
                FROM hypothesis_candidates
                WHERE repo_root = ? AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (repo_root,),
            ).fetchall()

    def ready_candidate_preference_json(
        self, repo_root: str, driver: str
    ) -> str | None:
        """The raw preference_json for a ready-to-surface candidate, or None.

        The surfacing layer needs the stored JSON (to read the `type` field —
        preference vs. behavioral_guideline — that the Preference object doesn't
        carry). Owning the query here keeps the candidate table's column shape
        inside the store rather than leaking it into the run layer.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT preference_json FROM hypothesis_candidates "
                "WHERE repo_root = ? AND driver = ? AND status = 'ready_to_surface' "
                "LIMIT 1",
                (repo_root, driver),
            ).fetchone()
        if row and row["preference_json"]:
            return row["preference_json"]
        return None

    def update_hypothesis_evidence(
        self,
        candidate_id: int,
        *,
        delta_for: int = 0,
        delta_against: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE hypothesis_candidates
                SET evidence_for = evidence_for + ?,
                    evidence_against = evidence_against + ?
                WHERE id = ?
                """,
                (delta_for, delta_against, candidate_id),
            )

    def set_hypothesis_status(self, candidate_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE hypothesis_candidates SET status = ? WHERE id = ?",
                (status, candidate_id),
            )

    def candidate_driver_exists(
        self, repo_root: str, session_id: str, driver: str
    ) -> bool:
        """True if a candidate with this driver is already in the bank for this repo.

        Repo-scoped to match get_pending_hypothesis_candidates: the bank's
        invariant is "no duplicate drivers per repo," not per session.
        Session_id retained in signature for legacy callers.
        """
        del session_id
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM hypothesis_candidates
                WHERE repo_root = ? AND driver = ?
                  AND status NOT IN ('pruned', 'rejected', 'declined')
                LIMIT 1
                """,
                (repo_root, driver),
            ).fetchone()
        return row is not None
