from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Lease:
    file_path: str
    expires_at: int | None
    lease_type: str


class TrustDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
