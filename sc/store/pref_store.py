from __future__ import annotations

"""PrefStoreMixin — autonomy preferences and confirmed preference hypotheses.

Methods: autonomy_preferences, merge_autonomy_preferences, revoke_autonomy_preference,
_save_autonomy_preferences, delete_autonomy_preferences, save_confirmed_preference,
session_has_confirmed_hypothesis, confirmed_preferences_for_repo,
confirmed_preferences_for_session, add_hypothesis_candidate,
get_pending_hypothesis_candidates, update_hypothesis_evidence,
set_hypothesis_status, candidate_driver_exists.
"""

import time


class PrefStoreMixin:
    # _connect and autonomy imports are provided by TrustDB.

    def autonomy_preferences(self, repo_root: str):
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
        inferred,
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

    def _save_autonomy_preferences(self, repo_root: str, preferences) -> None:
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
    ) -> list:
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
    ) -> list:
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
    ) -> int:
        """Insert a new pending candidate. Returns its id."""
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO hypothesis_candidates
                    (repo_root, session_id, driver, source, prompt, rationale,
                     preference_json, evidence_for, evidence_against, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 'pending', ?)
                """,
                (repo_root, session_id, driver, source, prompt, rationale, preference_json, now),
            )
        return int(cur.lastrowid)

    def get_pending_hypothesis_candidates(
        self, repo_root: str, session_id: str
    ) -> list:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, driver, source, prompt, rationale, preference_json,
                       evidence_for, evidence_against, status
                FROM hypothesis_candidates
                WHERE repo_root = ? AND session_id = ? AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (repo_root, session_id),
            ).fetchall()

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
        """True if a candidate with this driver is already in the bank for this session."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM hypothesis_candidates
                WHERE repo_root = ? AND session_id = ? AND driver = ?
                  AND status NOT IN ('pruned', 'rejected', 'declined')  -- legacy: 'pruned' kept for old DBs
                LIMIT 1
                """,
                (repo_root, session_id, driver),
            ).fetchone()
        return row is not None
