from __future__ import annotations

"""File co-change graph over decision_traces.

A file pair is considered to co-change if both files appear under the
same *task* (apply stage). Task is a better unit than session_id because
a single session can span multiple unrelated tasks, and seeded history
shares one session_id by construction. The graph is computed lazily —
no separate table, no migration. SQLite handles the query in
milliseconds at demo scale.
"""

from .trust_db import TrustDB

# Columns co-change may group by. The CLI groups by `task` (its natural unit);
# the plugin has no task string (it records task=repo_root), so it groups by
# `session_id` — edits in one Claude Code session are that path's "task". The
# column is interpolated into SQL, so it MUST come from this whitelist (never
# from caller-controlled input) to stay injection-safe.
_GROUP_COLS = {"task", "session_id"}


def cochanged_files(
    trust_db: TrustDB,
    repo_root: str,
    file_path: str,
    *,
    min_count: int = 2,
    limit: int = 3,
    group_col: str = "task",
) -> list[tuple[str, int]]:
    """Return up to *limit* files that historically appeared under the
    same *group_col* (task, or session_id for the plugin) as *file_path*,
    ranked by number of distinct co-occurring groups. Filters to apply-stage
    traces. Empty if nothing meets *min_count*.
    """
    g = group_col if group_col in _GROUP_COLS else "task"
    with trust_db._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT file_path, COUNT(DISTINCT {g}) AS n_groups
            FROM decision_traces
            WHERE repo_root = ?
              AND stage = 'apply'
              AND file_path != ?
              AND file_path != '__session__'
              AND {g} IS NOT NULL
              AND {g} != ''
              AND {g} IN (
                  SELECT DISTINCT {g}
                  FROM decision_traces
                  WHERE repo_root = ?
                    AND stage = 'apply'
                    AND file_path = ?
                    AND {g} IS NOT NULL
                    AND {g} != ''
              )
            GROUP BY file_path
            HAVING n_groups >= ?
            ORDER BY n_groups DESC, file_path ASC
            LIMIT ?
            """,
            (repo_root, file_path, repo_root, file_path, min_count, limit),
        ).fetchall()
    return [(r["file_path"], int(r["n_groups"])) for r in rows]


def cochange_graph(
    trust_db: TrustDB,
    repo_root: str,
    *,
    min_count: int = 2,
    limit_per_file: int = 3,
    group_col: str = "task",
) -> dict[str, list[tuple[str, int]]]:
    """Return the full co-change adjacency for the repo in a single query.

    Uses a self-join on *group_col* (task, or session_id for the plugin) so the
    whole graph is built in one round-trip instead of N+1 per-file queries.
    """
    g = group_col if group_col in _GROUP_COLS else "task"
    with trust_db._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT a.file_path AS src, b.file_path AS nbr,
                   COUNT(DISTINCT a.{g}) AS n_groups
            FROM decision_traces a
            JOIN decision_traces b
              ON  b.repo_root = a.repo_root
              AND b.{g}       = a.{g}
              AND b.stage     = 'apply'
              AND b.file_path != a.file_path
              AND b.file_path != '__session__'
              AND b.{g} IS NOT NULL
              AND b.{g} != ''
            WHERE a.repo_root = ?
              AND a.stage     = 'apply'
              AND a.file_path != '__session__'
              AND a.{g} IS NOT NULL
              AND a.{g} != ''
            GROUP BY a.file_path, b.file_path
            HAVING n_groups >= ?
            ORDER BY a.file_path ASC, n_groups DESC, b.file_path ASC
            """,
            (repo_root, min_count),
        ).fetchall()

    # Build adjacency dict, capping each source file to limit_per_file neighbours.
    graph: dict[str, list[tuple[str, int]]] = {}
    for r in rows:
        src = r["src"]
        if src not in graph:
            graph[src] = []
        if len(graph[src]) < limit_per_file:
            graph[src].append((r["nbr"], int(r["n_groups"])))
    return graph
