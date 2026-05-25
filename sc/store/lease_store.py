from __future__ import annotations

"""LeaseStoreMixin — write-lease and read-lease CRUD.

Methods: active_leases, active_read_leases, list_active_leases, add_leases,
add_permanent_leases, add_permanent_read_leases, revoke, approved_apply_counts.
Helpers: _best_lease_map, _dedupe_lease_rows.
"""

import json
import sqlite3
import time
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from ..trust_db import Lease


class LeaseStoreMixin:
    # _connect and dataclasses (Lease) are provided by TrustDB

    def _dedupe_lease_rows(self, conn: sqlite3.Connection, *, table: str) -> None:
        duplicate_keys = conn.execute(
            f"""
            SELECT repo_root, file_path
            FROM {table}
            GROUP BY repo_root, file_path
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for key in duplicate_keys:
            repo_root = str(key["repo_root"])
            file_path = str(key["file_path"])
            rows = conn.execute(
                f"""
                SELECT id, created_at, expires_at
                FROM {table}
                WHERE repo_root = ? AND file_path = ?
                """,
                (repo_root, file_path),
            ).fetchall()
            if len(rows) < 2:
                continue
            keep = max(
                rows,
                key=lambda row: (
                    row["expires_at"] is None,
                    int(row["expires_at"] or 0),
                    int(row["created_at"]),
                    int(row["id"]),
                ),
            )
            conn.execute(
                f"""
                DELETE FROM {table}
                WHERE repo_root = ? AND file_path = ? AND id <> ?
                """,
                (repo_root, file_path, int(keep["id"])),
            )

    def _best_lease_map(
        self,
        rows: Iterable[sqlite3.Row],
        lease_type: str,
    ) -> dict[str, "Lease"]:
        best: dict[str, "Lease"] = {}
        for row in rows:
            file_path = str(row["file_path"])
            expires_at = row["expires_at"]
            from ..trust_db import Lease
            candidate = Lease(file_path, expires_at, lease_type)
            existing = best.get(file_path)
            if existing is None:
                best[file_path] = candidate
                continue
            if existing.expires_at is None:
                continue
            if candidate.expires_at is None:
                best[file_path] = candidate
                continue
            if int(candidate.expires_at) > int(existing.expires_at):
                best[file_path] = candidate
        return best

    def active_leases(self, repo_root: str, files: Iterable[str]) -> dict[str, "Lease"]:
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
        return self._best_lease_map(rows, "write")

    def active_read_leases(self, repo_root: str, files: Iterable[str]) -> dict[str, "Lease"]:
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
        return self._best_lease_map(rows, "read")

    def list_active_leases(self, repo_root: str) -> list["Lease"]:
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
        write_map = self._best_lease_map(rows, "write")
        read_map = self._best_lease_map(read_rows, "read")
        leases = list(write_map.values())
        leases.extend(read_map.values())
        leases.sort(key=lambda item: (item.lease_type, item.file_path))
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
                ON CONFLICT(repo_root, file_path) DO UPDATE SET
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    source = excluded.source
                WHERE leases.expires_at IS NOT NULL
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
                ON CONFLICT(repo_root, file_path) DO UPDATE SET
                    created_at = excluded.created_at,
                    expires_at = NULL,
                    source = excluded.source
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
                ON CONFLICT(repo_root, file_path) DO UPDATE SET
                    created_at = excluded.created_at,
                    expires_at = NULL,
                    source = excluded.source
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
