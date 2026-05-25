from __future__ import annotations

"""ModelStoreMixin — PolicyClassifier persistence and snapshot management.

Methods: load_policy_model, save_policy_model, list_policy_model_snapshots,
restore_policy_model_snapshot, policy_model_sample_count, delete_policy_model.
Constant: _SNAPSHOT_RETENTION.
"""

import time


class ModelStoreMixin:
    # _connect is provided by TrustDB.

    # Keep this many snapshots per repo. Enough to roll back past a bad
    # session without letting the DB grow unbounded.
    _SNAPSHOT_RETENTION = 20

    def load_policy_model(self, repo_root: str):
        """Return the persisted PolicyClassifier for repo_root, or None if absent."""
        from ..ml_policy import PolicyClassifier  # local import to keep trust_db lean

        with self._connect() as conn:
            row = conn.execute(
                "SELECT model_blob FROM policy_models WHERE repo_root = ?",
                (repo_root,),
            ).fetchone()
        if row is None:
            return None
        try:
            return PolicyClassifier.from_bytes(bytes(row["model_blob"]))
        except Exception:
            return None

    def save_policy_model(self, repo_root: str, model) -> None:
        """Persist a PolicyClassifier for repo_root.

        Takes a snapshot of the *prior* model blob (if any) before writing
        the new one, so `restore_policy_model_snapshot` can roll back.
        """
        now = int(time.time())
        blob = model.to_bytes()
        with self._connect() as conn:
            prior = conn.execute(
                "SELECT model_blob, sample_count FROM policy_models WHERE repo_root = ?",
                (repo_root,),
            ).fetchone()
            if prior is not None:
                conn.execute(
                    """
                    INSERT INTO policy_model_snapshots
                        (repo_root, model_blob, sample_count, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (repo_root, bytes(prior["model_blob"]), int(prior["sample_count"]), now),
                )
                # Prune older snapshots beyond retention.
                conn.execute(
                    """
                    DELETE FROM policy_model_snapshots
                    WHERE repo_root = ? AND id NOT IN (
                        SELECT id FROM policy_model_snapshots
                        WHERE repo_root = ?
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                    )
                    """,
                    (repo_root, repo_root, self._SNAPSHOT_RETENTION),
                )
            conn.execute(
                """
                INSERT INTO policy_models (repo_root, model_blob, sample_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repo_root) DO UPDATE SET
                    model_blob = excluded.model_blob,
                    sample_count = excluded.sample_count,
                    updated_at = excluded.updated_at
                """,
                (repo_root, blob, model.sample_count, now),
            )

    def list_policy_model_snapshots(
        self, repo_root: str
    ) -> list[dict]:
        """Return snapshot metadata (not blobs) for display."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, sample_count, created_at
                FROM policy_model_snapshots
                WHERE repo_root = ?
                ORDER BY created_at DESC, id DESC
                """,
                (repo_root,),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "sample_count": int(r["sample_count"]),
                "created_at": int(r["created_at"]),
            }
            for r in rows
        ]

    def restore_policy_model_snapshot(
        self, repo_root: str, snapshot_id: int | None = None
    ) -> bool:
        """Restore the given snapshot (or the most recent one if None).

        Returns True on success, False if no matching snapshot exists.
        """
        now = int(time.time())
        with self._connect() as conn:
            if snapshot_id is None:
                row = conn.execute(
                    """
                    SELECT id, model_blob, sample_count FROM policy_model_snapshots
                    WHERE repo_root = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (repo_root,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id, model_blob, sample_count FROM policy_model_snapshots
                    WHERE repo_root = ? AND id = ?
                    """,
                    (repo_root, snapshot_id),
                ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                INSERT INTO policy_models (repo_root, model_blob, sample_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repo_root) DO UPDATE SET
                    model_blob = excluded.model_blob,
                    sample_count = excluded.sample_count,
                    updated_at = excluded.updated_at
                """,
                (repo_root, bytes(row["model_blob"]), int(row["sample_count"]), now),
            )
            # Consume the snapshot — rollbacks shouldn't be silently reapplied.
            conn.execute(
                "DELETE FROM policy_model_snapshots WHERE id = ?",
                (int(row["id"]),),
            )
        return True

    def policy_model_sample_count(self, repo_root: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sample_count FROM policy_models WHERE repo_root = ?",
                (repo_root,),
            ).fetchone()
        return int(row["sample_count"]) if row else 0

    def delete_policy_model(self, repo_root: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM policy_models WHERE repo_root = ?",
                (repo_root,),
            )
