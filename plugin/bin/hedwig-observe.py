#!/usr/bin/env python3
"""Hedwig observability surfaces for the plugin (/hedwig-weights, /hedwig-retrospective).

The plugin already records everything these read; this script just surfaces it
so a developer (or a booth visitor) can SEE the learning, not only trigger it:

    hedwig-observe.py weights        -> per-feature drift of the learned
                                        classifier from its cold-start baseline
                                        (which signals this repo's behavior has
                                        shifted, and in which direction)
    hedwig-observe.py retrospective  -> regret events: edits Hedwig auto-applied
                                        that were later reverted or failed
                                        verification (where it was too trusting)

Plain text on stdout (a slash command's output goes to the transcript, not a
Rich terminal). Always exits 0; local, no credentials. The weights view needs
the learned classifier materialized (numpy/sklearn) — if it can't load, it says
so and points at hedwig-setup.py rather than erroring.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _hedwig_common import (  # noqa: E402
    _iter_jsonl,
    learned_scorer_reachable,
    open_trust_db,
    repo_root_key,
)

# Human-readable labels, mirroring the CLI's `hw observe weights` (observe.py).
_LABELS: dict[str, str] = {
    "prior_approvals": "prior approvals",
    "prior_denials": "prior denials",
    "avg_response_ms": "avg review time",
    "avg_edit_distance": "avg edit distance",
    "diff_size_log": "diff size (log)",
    "blast_radius": "blast radius",
    "is_new_file": "new file",
    "is_security_sensitive": "security sensitive",
    "files_in_action": "files in action",
    "recent_denials": "recent denials",
    "verification_failure_rate": "verification failure rate",
    "model_confidence_avg": "model confidence",
    "change_pattern_risk": "change pattern risk",
    "model_risk_score": "model reviewer (advisory)",
}


def _cmd_weights() -> int:
    if not learned_scorer_reachable():
        sys.stdout.write(
            "The learned classifier isn't active here, so there are no weights to "
            "show yet — Hedwig is running the stdlib heuristic.\n"
            "Turn it on once with: python3 plugin/bin/hedwig-setup.py\n"
        )
        return 0
    repo = repo_root_key(None)
    try:
        from sc.ml_policy import MIN_SAMPLES_FOR_LEARNED  # noqa: PLC0415

        db = open_trust_db()
        classifier = db.load_policy_model(repo)
        if classifier is None:
            sys.stdout.write(
                "No classifier recorded for this repo yet. Make a few edits and "
                "re-run /hedwig-weights.\n"
            )
            return 0
        deltas = classifier.coef_delta()  # {feature: signed drift from cold-seed}
        samples = classifier.sample_count
    except Exception as exc:
        sys.stdout.write(f"Could not read classifier weights: {exc}\n")
        return 0

    active = samples >= MIN_SAMPLES_FOR_LEARNED
    lines = [
        f"Hedwig — learned classifier drift ({samples} real decisions; "
        f"{'learned scorer ACTIVE' if active else f'heuristic until {MIN_SAMPLES_FOR_LEARNED}'})",
        "",
    ]
    # Show features that have drifted, largest-magnitude first.
    ranked = sorted(deltas.items(), key=lambda kv: abs(kv[1]), reverse=True)
    significant = [(k, v) for k, v in ranked if abs(v) > 0.01]
    shown = significant or ranked[:3]
    if not significant:
        lines.append("  No meaningful drift yet — the classifier is still near its")
        lines.append("  cold-start baseline. Drift appears as real decisions accumulate.")
        lines.append("")
    for name, delta in shown:
        label = _LABELS.get(name, name)
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "·")
        direction = "more trusting" if delta > 0 else "more cautious"
        lines.append(f"  {arrow} {label:<26} {delta:+.3f}  ({direction})")
    lines.append("")
    lines.append(
        "  ▲ = this signal now pushes toward auto-apply; ▼ = toward a check-in. "
        "All drift is from this repo's real decisions — no weight was hand-tuned."
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def _cmd_retrospective() -> int:
    repo = repo_root_key(None)
    # regret.jsonl rows: {session_id, cwd, files:[...], signal:"reversal" | verify_cmd:...}
    events = []
    for row in _iter_jsonl("regret.jsonl"):
        # Scope to this repo when the row recorded a cwd (older rows may not).
        row_cwd = row.get("cwd")
        if row_cwd and repo_root_key(row_cwd) != repo:
            continue
        files = row.get("files") or []
        kind = "reverted" if row.get("signal") == "reversal" else "failed verification"
        for f in files:
            events.append((f, kind))
    if not events:
        sys.stdout.write(
            "No regret events yet — Hedwig hasn't auto-applied an edit that was "
            "later reverted or failed verification in this repo.\n"
            "This is the signal that Hedwig was too trusting; an empty list is good.\n"
        )
        return 0
    lines = [
        f"Hedwig — retrospective: {len(events)} regret event"
        f"{'s' if len(events) != 1 else ''} (auto-applied, then corrected)",
        "",
    ]
    # Most recent first; de-dupe identical (file, kind) pairs.
    seen: set = set()
    for f, kind in reversed(events):
        key = (f, kind)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  • {f} — {kind} after auto-apply")
        if len(seen) >= 10:
            break
    lines.append("")
    lines.append(
        "  Each of these tightened Hedwig's next decision on that file (and, via "
        "the classifier, on risk-similar edits elsewhere)."
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def main(argv: list[str]) -> int:
    cmd = (argv[0].lower() if argv else "weights")
    if cmd == "retrospective":
        return _cmd_retrospective()
    if cmd == "weights":
        return _cmd_weights()
    sys.stdout.write(
        "Usage:\n"
        "  /hedwig-weights         (classifier drift)\n"
        "  /hedwig-retrospective   (regret events)\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
