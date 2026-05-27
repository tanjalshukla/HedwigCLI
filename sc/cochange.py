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


def cochanged_files(
    trust_db: TrustDB,
    repo_root: str,
    file_path: str,
    *,
    min_count: int = 2,
    limit: int = 3,
) -> list[tuple[str, int]]:
    """Return up to *limit* files that historically appeared under the
    same task as *file_path*, ranked by number of distinct co-occurring
    tasks. Filters to apply-stage traces. Empty if nothing meets
    *min_count*.
    """
    with trust_db._connect() as conn:
        rows = conn.execute(
            """
            SELECT file_path, COUNT(DISTINCT task) AS n_tasks
            FROM decision_traces
            WHERE repo_root = ?
              AND stage = 'apply'
              AND file_path != ?
              AND file_path != '__session__'
              AND task IS NOT NULL
              AND task != ''
              AND task IN (
                  SELECT DISTINCT task
                  FROM decision_traces
                  WHERE repo_root = ?
                    AND stage = 'apply'
                    AND file_path = ?
                    AND task IS NOT NULL
                    AND task != ''
              )
            GROUP BY file_path
            HAVING n_tasks >= ?
            ORDER BY n_tasks DESC, file_path ASC
            LIMIT ?
            """,
            (repo_root, file_path, repo_root, file_path, min_count, limit),
        ).fetchall()
    return [(r["file_path"], int(r["n_tasks"])) for r in rows]


def cochange_graph(
    trust_db: TrustDB,
    repo_root: str,
    *,
    min_count: int = 2,
    limit_per_file: int = 3,
) -> dict[str, list[tuple[str, int]]]:
    """Return the full co-change adjacency for the repo in a single query.

    Uses a self-join on task so the whole graph is built in one round-trip
    instead of N+1 per-file queries.
    """
    with trust_db._connect() as conn:
        rows = conn.execute(
            """
            SELECT a.file_path AS src, b.file_path AS nbr,
                   COUNT(DISTINCT a.task) AS n_tasks
            FROM decision_traces a
            JOIN decision_traces b
              ON  b.repo_root = a.repo_root
              AND b.task      = a.task
              AND b.stage     = 'apply'
              AND b.file_path != a.file_path
              AND b.file_path != '__session__'
              AND b.task IS NOT NULL
              AND b.task != ''
            WHERE a.repo_root = ?
              AND a.stage     = 'apply'
              AND a.file_path != '__session__'
              AND a.task IS NOT NULL
              AND a.task != ''
            GROUP BY a.file_path, b.file_path
            HAVING n_tasks >= ?
            ORDER BY a.file_path ASC, n_tasks DESC, b.file_path ASC
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
            graph[src].append((r["nbr"], int(r["n_tasks"])))
    return graph
