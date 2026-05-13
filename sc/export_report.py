from __future__ import annotations

"""Generate a single-file HTML report with researcher-grade Hedwig data.

Philosophy: the terminal shows a developer what they need. This HTML shows
a researcher (or the developer going deep) the structured data underneath.
Inline CSS, no external dependencies — single file, openable anywhere.
"""

import html
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .ml_policy import FEATURE_NAMES
from .preference_inference import (
    infer_coding_mode,
    infer_user_persona,
    summarize_session,
)
from .preferences import FAILURE_SIGNAL_CHECKIN
from .trust_db import TrustDB


CSS = """
* { box-sizing: border-box; }
html, body {
    margin: 0;
    padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    color: #1a1a1a;
    background: #fafafa;
    line-height: 1.5;
}
main {
    max-width: 1100px;
    margin: 0 auto;
    padding: 48px 32px;
}
header {
    margin-bottom: 40px;
    border-bottom: 1px solid #e5e5e5;
    padding-bottom: 24px;
}
h1 {
    font-size: 28px;
    font-weight: 600;
    margin: 0 0 8px 0;
    letter-spacing: -0.02em;
}
.subtitle {
    color: #666;
    font-size: 14px;
}
h2 {
    font-size: 20px;
    font-weight: 600;
    margin: 40px 0 16px 0;
    letter-spacing: -0.01em;
}
h3 {
    font-size: 14px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #666;
    margin: 24px 0 12px 0;
}
.card {
    background: white;
    border: 1px solid #e5e5e5;
    border-radius: 8px;
    padding: 20px 24px;
    margin-bottom: 16px;
}
.kv {
    display: grid;
    grid-template-columns: 180px 1fr;
    gap: 8px 16px;
    margin: 0;
}
.kv dt { color: #666; font-size: 14px; }
.kv dd { margin: 0; font-weight: 500; }
table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
}
th, td {
    text-align: left;
    padding: 10px 12px;
    border-bottom: 1px solid #eee;
}
th {
    font-weight: 600;
    color: #666;
    text-transform: uppercase;
    font-size: 12px;
    letter-spacing: 0.05em;
}
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.bar {
    display: inline-block;
    height: 8px;
    border-radius: 4px;
    vertical-align: middle;
    margin-right: 6px;
}
.bar-pos { background: #22c55e; }
.bar-neg { background: #ef4444; }
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 500;
    margin-right: 4px;
}
.badge-approve { background: #dcfce7; color: #166534; }
.badge-deny { background: #fee2e2; color: #991b1b; }
.badge-flag { background: #fef3c7; color: #854d0e; }
.badge-info { background: #dbeafe; color: #1e40af; }
.badge-learn { background: #f3e8ff; color: #6b21a8; }
.badge-meta { background: #f3f4f6; color: #4b5563; }
.empty { color: #999; font-style: italic; padding: 16px 0; }
footer {
    margin-top: 64px;
    padding-top: 24px;
    border-top: 1px solid #e5e5e5;
    color: #999;
    font-size: 12px;
}
.owl {
    font-family: ui-monospace, "SF Mono", Monaco, Consolas, monospace;
    white-space: pre;
    color: #0891b2;
    font-size: 12px;
    line-height: 1.2;
    display: inline-block;
    margin-right: 16px;
    vertical-align: middle;
}
.header-flex { display: flex; align-items: center; gap: 12px; }
"""

OWL_HTML = """   ,___,
   (O,O)
   (   )
   -&quot;-&quot;-"""


@dataclass
class SessionSnapshot:
    session_id: str
    started_at: str
    turns: int
    approvals: int
    corrections: int
    denials: int
    failures: int
    coding_mode: str
    intensity: str


def _esc(s: str | None) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


def _sessions_for_repo(trust_db: TrustDB, repo_root: str) -> list[SessionSnapshot]:
    with trust_db._connect() as conn:
        rows = conn.execute(
            """
            SELECT session_id, MIN(created_at) AS started_at, MAX(created_at) AS ended_at
            FROM decision_traces
            WHERE repo_root = ?
            GROUP BY session_id
            ORDER BY ended_at DESC
            LIMIT 50
            """,
            (repo_root,),
        ).fetchall()
    out: list[SessionSnapshot] = []
    for r in rows:
        sid = r["session_id"]
        session_rows = trust_db.session_traces(repo_root, sid)
        row_dicts = [dict(rr) for rr in session_rows]
        summary = summarize_session(row_dicts)
        started = datetime.fromtimestamp(r["started_at"]).strftime("%Y-%m-%d %H:%M")
        out.append(
            SessionSnapshot(
                session_id=sid,
                started_at=started,
                turns=summary.n_turns,
                approvals=summary.n_approvals,
                corrections=summary.n_feedback,
                denials=summary.n_denials,
                failures=summary.n_failures,
                coding_mode=infer_coding_mode(summary).value,
                intensity=infer_user_persona(summary).value,
            )
        )
    return out


def _coefficient_table(trust_db: TrustDB, repo_root: str) -> str:
    classifier = trust_db.load_policy_model(repo_root)
    if classifier is None:
        return '<p class="empty">No learned model for this repo.</p>'

    sample_count = classifier.sample_count
    personalized = sample_count >= 10
    current = classifier.clf.coef_[0]
    prior = classifier.prior_coef
    max_abs = max((abs(current[i] - prior[i]) for i in range(len(FEATURE_NAMES))), default=0.0) or 1.0

    rows: list[str] = []
    rows.append("<thead><tr><th>Feature</th><th>Prior</th><th>Current</th><th>Delta</th><th>Drift</th></tr></thead>")
    rows.append("<tbody>")
    for i, name in enumerate(FEATURE_NAMES):
        p = prior[i]
        c = current[i]
        d = c - p
        bar_width = int(round(abs(d) / max_abs * 80))
        bar_class = "bar-pos" if d > 0 else ("bar-neg" if d < 0 else "")
        bar_html = (
            f'<div class="bar {bar_class}" style="width: {bar_width}px"></div>'
            if bar_class else ""
        )
        rows.append(
            f"<tr><td>{_esc(name)}</td>"
            f'<td class="num">{p:+.3f}</td>'
            f'<td class="num">{c:+.3f}</td>'
            f'<td class="num">{d:+.3f}</td>'
            f"<td>{bar_html}</td></tr>"
        )
    rows.append("</tbody>")
    return (
        f'<p style="color:#666; font-size:14px; margin-top:0;">'
        f"{sample_count} real decisions incorporated. "
        f"{'Learned scorer active.' if personalized else 'Heuristic scorer still active.'}</p>"
        f"<table>{''.join(rows)}</table>"
    )


def _sessions_table(sessions: list[SessionSnapshot]) -> str:
    if not sessions:
        return '<p class="empty">No sessions yet.</p>'
    rows: list[str] = []
    rows.append(
        "<thead><tr>"
        "<th>Session</th><th>Started</th><th>Turns</th><th>Mode</th>"
        "<th>Style</th><th>Approvals</th><th>Corrections</th>"
        "<th>Denials</th><th>Failures</th>"
        "</tr></thead>"
    )
    rows.append("<tbody>")
    for s in sessions:
        rows.append(
            f"<tr><td><code>{_esc(s.session_id[:10])}…</code></td>"
            f"<td>{_esc(s.started_at)}</td>"
            f'<td class="num">{s.turns}</td>'
            f'<td><span class="badge badge-info">{_esc(s.coding_mode)}</span></td>'
            f'<td><span class="badge badge-learn">{_esc(s.intensity)}</span></td>'
            f'<td class="num">{s.approvals}</td>'
            f'<td class="num">{s.corrections}</td>'
            f'<td class="num">{s.denials}</td>'
            f'<td class="num">{s.failures}</td></tr>'
        )
    rows.append("</tbody>")
    return f"<table>{''.join(rows)}</table>"


def _learned_preferences_section(trust_db: TrustDB, repo_root: str) -> str:
    """List every confirmed preference across all sessions in this repo."""
    with trust_db._connect() as conn:
        rows = conn.execute(
            """
            SELECT session_id, preference_json, driver, created_at
            FROM confirmed_preferences
            WHERE repo_root = ?
            ORDER BY created_at DESC
            """,
            (repo_root,),
        ).fetchall()

    if not rows:
        return '<p class="empty">No confirmed preferences yet.</p>'

    items: list[str] = []
    items.append("<ul style='padding-left:20px; margin:0;'>")
    for r in rows:
        try:
            payload = json.loads(r["preference_json"])
        except Exception:
            continue
        if not payload.get("accepted"):
            continue
        driver = payload.get("driver", "unknown")
        when = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        session_short = r["session_id"][:8] if r["session_id"] else "—"
        items.append(
            f"<li style='margin-bottom:12px;'>"
            f"<strong>{_esc(driver)}</strong> "
            f"<span class='badge badge-meta'>session {_esc(session_short)}</span> "
            f"<span style='color:#999; font-size:12px;'>{_esc(when)}</span>"
            f"</li>"
        )
    items.append("</ul>")
    return "".join(items)


def _trigger_firings_section(trust_db: TrustDB, repo_root: str) -> str:
    """Report when the built-in failure-signal trigger would have fired across
    all sessions in this repo."""
    sessions = _sessions_for_repo(trust_db, repo_root)
    min_failures = FAILURE_SIGNAL_CHECKIN.condition.min_prior_failure_count or 0
    fired_sessions = [s for s in sessions if s.failures >= min_failures and min_failures > 0]
    total = len(sessions)
    fired = len(fired_sessions)

    parts: list[str] = []
    parts.append(
        f'<p>Across <strong>{total}</strong> sessions in this repo, the built-in '
        f'failure-signal check-in would have fired in <strong>{fired}</strong> '
        f'({fired * 100 // total if total else 0}%).</p>'
    )
    if fired_sessions:
        parts.append("<ul style='padding-left:20px; margin:0;'>")
        for s in fired_sessions[:10]:
            parts.append(
                f"<li><code>{_esc(s.session_id[:10])}…</code> · {s.turns} turns · "
                f"{s.failures} failure report{'s' if s.failures != 1 else ''}</li>"
            )
        parts.append("</ul>")
    return "".join(parts)


def _decision_distribution(trust_db: TrustDB, repo_root: str) -> str:
    with trust_db._connect() as conn:
        rows = conn.execute(
            "SELECT user_decision, COUNT(*) AS n FROM decision_traces "
            "WHERE repo_root = ? GROUP BY user_decision ORDER BY n DESC",
            (repo_root,),
        ).fetchall()
    if not rows:
        return '<p class="empty">No decisions recorded yet.</p>'
    out: list[str] = ["<table>"]
    out.append("<thead><tr><th>Decision</th><th>Count</th></tr></thead><tbody>")
    for r in rows:
        out.append(f"<tr><td>{_esc(r['user_decision'])}</td><td class='num'>{r['n']}</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _pushback_distribution(trust_db: TrustDB, repo_root: str) -> str:
    with trust_db._connect() as conn:
        rows = conn.execute(
            "SELECT pushback_type, COUNT(*) AS n FROM decision_traces "
            "WHERE repo_root = ? AND pushback_type IS NOT NULL "
            "GROUP BY pushback_type ORDER BY n DESC",
            (repo_root,),
        ).fetchall()
    if not rows:
        return '<p class="empty">No pushback classifications recorded yet.</p>'
    out: list[str] = ["<table>"]
    out.append("<thead><tr><th>Pushback category</th><th>Count</th></tr></thead><tbody>")
    for r in rows:
        out.append(f"<tr><td>{_esc(r['pushback_type'])}</td><td class='num'>{r['n']}</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def generate_html_report(trust_db: TrustDB, repo_root: str) -> str:
    """Return the full HTML report as a single string."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sessions = _sessions_for_repo(trust_db, repo_root)

    parts: list[str] = []
    parts.append("<!DOCTYPE html><html><head><meta charset='utf-8'>")
    parts.append(f"<title>Hedwig report · {_esc(repo_root)}</title>")
    parts.append(f"<style>{CSS}</style>")
    parts.append("</head><body><main>")

    parts.append("<header>")
    parts.append("<div class='header-flex'>")
    parts.append(f"<div class='owl'>{OWL_HTML}</div>")
    parts.append("<div>")
    parts.append("<h1>Hedwig report</h1>")
    parts.append(
        f"<div class='subtitle'>Repo: <code>{_esc(repo_root)}</code> · Generated {_esc(now)}</div>"
    )
    parts.append("</div></div>")
    parts.append("</header>")

    # Sessions.
    parts.append("<h2>Sessions</h2>")
    parts.append("<div class='card'>")
    parts.append(_sessions_table(sessions))
    parts.append("</div>")

    # Learned preferences.
    parts.append("<h2>Confirmed preferences</h2>")
    parts.append("<div class='card'>")
    parts.append(_learned_preferences_section(trust_db, repo_root))
    parts.append("</div>")

    # Failure-signal firings.
    parts.append("<h2>Failure-signal check-in</h2>")
    parts.append("<div class='card'>")
    parts.append(
        "<p style='color:#666; font-size:14px; margin-top:0;'>"
        "Grounded in the SWE-chat analysis: debug intent + heavy shell activity + "
        "prior failure in the session predicts failure reports at 3.4× baseline.</p>"
    )
    parts.append(_trigger_firings_section(trust_db, repo_root))
    parts.append("</div>")

    # Learned coefficient drift.
    parts.append("<h2>Learned scorer · coefficient drift</h2>")
    parts.append("<div class='card'>")
    parts.append(_coefficient_table(trust_db, repo_root))
    parts.append("</div>")

    # Decisions + pushback.
    parts.append("<h2>Aggregate decisions</h2>")
    parts.append("<div class='card'>")
    parts.append(_decision_distribution(trust_db, repo_root))
    parts.append("</div>")

    parts.append("<h2>Pushback classifications</h2>")
    parts.append("<div class='card'>")
    parts.append(_pushback_distribution(trust_db, repo_root))
    parts.append("</div>")

    parts.append(
        "<footer>Hedwig · governance layer for coding agents · "
        "generated from <code>.sc/trust.db</code></footer>"
    )
    parts.append("</main></body></html>")
    return "".join(parts)


def write_report(trust_db: TrustDB, repo_root: str, out_path: Path) -> Path:
    """Write the report to ``out_path`` and return it."""
    html_str = generate_html_report(trust_db, repo_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_str)
    return out_path
