from __future__ import annotations

"""RuleStoreMixin — hard constraints, behavioral guidelines, logic notes, and retrieval.

Methods: replace_constraints, add_constraints, list_constraints, matching_constraints,
strongest_constraint, delete_constraints, replace_behavioral_guidelines,
add_behavioral_guidelines, list_behavioral_guidelines, delete_behavioral_guidelines,
add_logic_notes, recent_logic_notes, relevant_logic_notes, guideline_candidates,
relevant_behavioral_guidelines, relevant_feedback_snippets, recent_feedback_snippets.
Helpers: _retrieval_tokens, _overlap_score (module-level in trust_db, delegated here as class methods).
"""

import json
import re
import time
from fnmatch import fnmatch
from typing import Iterable

_RETRIEVAL_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "before", "by", "do", "for",
    "from", "if", "in", "into", "is", "it", "its", "of", "on", "or", "same",
    "should", "that", "the", "then", "this", "to", "use", "with",
}


def _retrieval_tokens(*parts: str | None) -> set[str]:
    tokens: set[str] = set()
    for part in parts:
        if not part:
            continue
        for token in re.findall(r"[a-z0-9_]+", part.lower()):
            if len(token) < 3 or token in _RETRIEVAL_STOPWORDS:
                continue
            tokens.add(token)
    return tokens


def _overlap_score(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not query_tokens or not candidate_tokens:
        return 0.0
    score = 0.0
    for token in query_tokens & candidate_tokens:
        score += 1.5 if len(token) >= 7 else 1.0
    return score


class RuleStoreMixin:
    # _connect and dataclasses (HardConstraint, BehavioralGuideline, LogicNote,
    # GuidelineCandidate) are provided by TrustDB.

    def replace_constraints(
        self,
        repo_root: str,
        source: str,
        constraints: Iterable,
    ) -> int:
        now = int(time.time())
        unique: dict[tuple, object] = {}
        for constraint in constraints:
            key = (
                constraint.path_pattern,
                str(constraint.read_policy),
                str(constraint.write_policy),
                constraint.source,
            )
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
                    repo_root, path_pattern, constraint_type, read_policy, write_policy, source, overridable, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        repo_root,
                        item.path_pattern,
                        item.constraint_type,
                        item.read_policy,
                        item.write_policy,
                        item.source,
                        1 if item.overridable else 0,
                        now,
                    )
                    for item in unique.values()
                ],
            )
        return len(unique)

    def add_constraints(
        self,
        repo_root: str,
        constraints: Iterable,
    ) -> int:
        """Append new hard constraints without deleting existing ones."""
        now = int(time.time())
        items: dict[tuple, object] = {}
        for constraint in constraints:
            key = (
                constraint.path_pattern,
                str(constraint.read_policy),
                str(constraint.write_policy),
                constraint.source,
            )
            items[key] = constraint
        if not items:
            return 0

        existing = {
            (
                item.path_pattern,
                str(item.read_policy),
                str(item.write_policy),
                item.source,
            )
            for item in self.list_constraints(repo_root)
        }
        pending = [item for key, item in items.items() if key not in existing]
        if not pending:
            return 0

        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO hard_constraints (
                    repo_root, path_pattern, constraint_type, read_policy, write_policy, source, overridable, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        repo_root,
                        item.path_pattern,
                        item.constraint_type,
                        item.read_policy,
                        item.write_policy,
                        item.source,
                        1 if item.overridable else 0,
                        now,
                    )
                    for item in pending
                ],
            )
        return len(pending)

    def list_constraints(self, repo_root: str) -> list:
        from ..trust_db import HardConstraint
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT path_pattern, constraint_type, read_policy, write_policy, source, overridable
                FROM hard_constraints
                WHERE repo_root = ?
                ORDER BY path_pattern, source, id
                """,
                (repo_root,),
            ).fetchall()
        return [
            HardConstraint(
                path_pattern=row["path_pattern"],
                source=row["source"],
                overridable=bool(row["overridable"]),
                constraint_type=row["constraint_type"],
                read_policy=row["read_policy"],
                write_policy=row["write_policy"],
            )
            for row in rows
        ]

    def matching_constraints(self, repo_root: str, file_path: str) -> list:
        all_constraints = self.list_constraints(repo_root)
        return [constraint for constraint in all_constraints if fnmatch(file_path, constraint.path_pattern)]

    def strongest_constraint(self, repo_root: str, file_path: str, *, access_type: str = "write"):
        priority = {"always_deny": 3, "always_check_in": 2, "always_allow": 1}
        matched = self.matching_constraints(repo_root, file_path)
        if not matched:
            return None
        return max(matched, key=lambda item: priority.get(item.policy_for(access_type), 0))

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

    def list_behavioral_guidelines(self, repo_root: str) -> list:
        from ..trust_db import BehavioralGuideline
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

    def add_logic_notes(
        self,
        repo_root: str,
        *,
        source: str,
        notes: Iterable[str],
        files: Iterable[str],
        change_types: Iterable[str] | None = None,
    ) -> int:
        items = [item.strip() for item in dict.fromkeys(notes) if item.strip()]
        if not items:
            return 0
        files_json = json.dumps(sorted(dict.fromkeys(files)))
        change_types_json = json.dumps(sorted(dict.fromkeys(change_types or [])))
        now = int(time.time())
        inserted = 0
        with self._connect() as conn:
            for note in items:
                existing = conn.execute(
                    """
                    SELECT 1
                    FROM logic_notes
                    WHERE repo_root = ? AND note = ?
                    LIMIT 1
                    """,
                    (repo_root, note),
                ).fetchone()
                if existing is not None:
                    continue
                conn.execute(
                    """
                    INSERT INTO logic_notes (
                        repo_root, note, files_json, change_types_json, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (repo_root, note, files_json, change_types_json, source, now),
                )
                inserted += 1
        return inserted

    def recent_logic_notes(self, repo_root: str, limit: int = 3) -> list:
        from ..trust_db import LogicNote
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT note, source
                FROM logic_notes
                WHERE repo_root = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (repo_root, max(limit, 1)),
            ).fetchall()
        return [LogicNote(note=str(row["note"]), source=str(row["source"])) for row in rows]

    def relevant_logic_notes(
        self,
        repo_root: str,
        *,
        query_text: str,
        spec_text: str | None = None,
        limit: int = 3,
        search_limit: int = 80,
    ) -> list:
        from ..trust_db import LogicNote
        query_tokens = _retrieval_tokens(query_text, spec_text)
        if not query_tokens:
            return self.recent_logic_notes(repo_root, limit=limit)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT note, files_json, change_types_json, source, created_at, id
                FROM logic_notes
                WHERE repo_root = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (repo_root, max(search_limit, 1)),
            ).fetchall()

        ranked: list[tuple[float, int, object]] = []
        for rank, row in enumerate(rows):
            note = str(row["note"]).strip()
            if not note:
                continue
            files = " ".join(json.loads(row["files_json"] or "[]"))
            change_types = " ".join(json.loads(row["change_types_json"] or "[]"))
            score = _overlap_score(query_tokens, _retrieval_tokens(note)) * 2.5
            score += _overlap_score(query_tokens, _retrieval_tokens(files)) * 0.75
            score += _overlap_score(query_tokens, _retrieval_tokens(change_types)) * 1.0
            score += max(0.0, 0.2 - (rank * 0.01))
            ranked.append(
                (
                    score,
                    rank,
                    LogicNote(note=note[:280], source=str(row["source"])),
                )
            )

        ranked.sort(key=lambda row: (-row[0], row[1], row[2].note))
        selected = [item for score, _, item in ranked if score > 0][: max(limit, 1)]
        if len(selected) >= max(limit, 1):
            return selected

        seen = {item.note for item in selected}
        for item in self.recent_logic_notes(repo_root, limit=limit):
            if item.note in seen:
                continue
            selected.append(item)
            if len(selected) >= max(limit, 1):
                break
        return selected

    def guideline_candidates(
        self,
        repo_root: str,
        *,
        min_count: int = 2,
        max_items: int = 8,
    ) -> list:
        """Suggest guidelines from repeated corrective developer feedback in traces."""
        from ..trust_db import GuidelineCandidate
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_feedback_text
                FROM decision_traces
                WHERE repo_root = ?
                  AND user_decision IN ('deny', 'revise')
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

    def relevant_behavioral_guidelines(
        self,
        repo_root: str,
        *,
        query_text: str,
        spec_text: str | None = None,
        limit: int = 8,
    ) -> list:
        items = self.list_behavioral_guidelines(repo_root)
        query_tokens = _retrieval_tokens(query_text, spec_text)
        if not query_tokens:
            return items[: max(limit, 1)]

        ranked: list[tuple[float, int, object]] = []
        for idx, item in enumerate(items):
            score = _overlap_score(query_tokens, _retrieval_tokens(item.guideline)) * 2.0
            score += max(0.0, 0.1 - (idx * 0.01))
            ranked.append((score, idx, item))

        ranked.sort(key=lambda row: (-row[0], row[1], row[2].guideline))
        selected = [item for score, _, item in ranked if score > 0][: max(limit, 1)]
        if len(selected) >= max(limit, 1):
            return selected
        seen = {item.guideline for item in selected}
        for item in items:
            if item.guideline in seen:
                continue
            selected.append(item)
            if len(selected) >= max(limit, 1):
                break
        return selected

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

    def relevant_feedback_snippets(
        self,
        repo_root: str,
        *,
        query_text: str,
        spec_text: str | None = None,
        limit: int = 4,
        search_limit: int = 200,
    ) -> list[str]:
        query_tokens = _retrieval_tokens(query_text, spec_text)
        if not query_tokens:
            return self.recent_feedback_snippets(repo_root, limit=limit)

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_feedback_text, file_path, change_type, created_at, id
                FROM decision_traces
                WHERE repo_root = ?
                  AND user_feedback_text IS NOT NULL
                  AND TRIM(user_feedback_text) != ''
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (repo_root, max(search_limit, 1)),
            ).fetchall()

        ranked: list[tuple[float, int, str]] = []
        seen: set[str] = set()
        for rank, row in enumerate(rows):
            text = " ".join(str(row["user_feedback_text"]).split()).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            score = _overlap_score(query_tokens, _retrieval_tokens(text)) * 2.0
            score += _overlap_score(query_tokens, _retrieval_tokens(str(row["file_path"]))) * 0.75
            score += _overlap_score(query_tokens, _retrieval_tokens(str(row["change_type"] or ""))) * 1.25
            score += max(0.0, 0.25 - (rank * 0.01))
            ranked.append((score, rank, text[:220]))

        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        selected = [text for score, _, text in ranked if score > 0][: max(limit, 1)]
        if len(selected) >= max(limit, 1):
            return selected

        fallback = self.recent_feedback_snippets(repo_root, limit=limit)
        for item in fallback:
            if item not in selected:
                selected.append(item)
            if len(selected) >= max(limit, 1):
                break
        return selected
