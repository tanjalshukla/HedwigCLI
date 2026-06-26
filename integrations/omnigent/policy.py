"""Hedwig as an Omnigent custom Python policy (R4 integration spike).

Omnigent (Databricks AI team + Neon, Apache-2.0, alpha,
`github.com/omnigent-ai/omnigent`) is a meta-harness with a first-class
"custom Python policy" extension point. Its policies are *authored by users*
with no mechanism that learns from outcomes; decisions are ALLOW / ASK / DENY.

This module plugs Hedwig's existing decide logic into that socket so a
decision that *adapts from this repo's per-file outcome history* runs inside
their harness. We reuse `sc/` directly — `assess_risk()` builds the
`RiskSignals`, `TrustDB.policy_history()` supplies the per-file outcome
history, and `HeuristicScorer` scores them. Nothing here re-implements
scoring.

What "adapts from outcomes" means here (and what it does NOT): the decision is
driven by the `HeuristicScorer` reading per-file `decision_traces` history —
recorded denials/regret on a file tighten the next like-action on that file
(the `-0.7` denial weight in `sc/policy.py`). That is "learns from outcomes"
in the per-file sense. The cross-file *generalizing* classifier
(`sc/ml_policy.PolicyClassifier`) is a separate Tier-1 story and is
deliberately NOT used here (no overclaim — see README G4 note).

------------------------------------------------------------------------------
VERIFY AGAINST REAL OMNIGENT API
------------------------------------------------------------------------------
The Omnigent-facing contract below was read from their `main` branch on
2026-06-25 (sources cited in README.md): a policy is
`def policy(event: PolicyEvent) -> PolicyResponse | None`, where `event` is a
dict with `type` (phase, e.g. "tool_call"), `data` = `{"name": tool,
"arguments": {...}}`, and `context` (dict carrying actor/usage and, per our
assumption, repo cwd), and the return is a dict
`{"result": "ALLOW"|"ASK"|"DENY", "reason": str}` (or `None` to abstain).
File paths live in `arguments` under `path` (Omnigent tools) or `file_path`
(Claude-native tools); shell commands under `command`. Omnigent is alpha and
external — its event shape may drift. Everything that touches the Omnigent
event/response shape is isolated in `_extract_action()` and `_response()`
below so QC can re-point it against a live install without touching the
Hedwig-side logic. See README "What QC must verify live".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sc.features import assess_risk
from sc.policy import HeuristicScorer, PolicyInput
from sc.trust_db import TrustDB

# Map Hedwig's PolicyAction onto Omnigent's verdict enum. 1:1 by design
# (the migration doc's "VERIFIED" competitive note): proceed -> ALLOW,
# proceed_flag -> ALLOW (auto-applied, flagged for observability, not a pause),
# check_in -> ASK. Hedwig's heuristic scorer never emits a hard "deny"; a hard
# DENY in Hedwig comes from a CLI hard-constraint, not the scorer, so it is not
# produced on this scorer-only path. The mapping table is documented in
# README.md.
_VERDICT = {
    "proceed": "ALLOW",
    "proceed_flag": "ALLOW",
    "check_in": "ASK",
}

# Thresholds mirror the plugin decide path (plugin/bin/hedwig-decide.py): a
# score >= 0.0 proceeds, otherwise the action surfaces. flag_threshold is set
# below proceed_threshold so the scorer collapses to the two-outcome
# proceed/check_in shape the plugin uses (no separate flag tier in the spike).
_PROCEED_THRESHOLD = 0.0
_FLAG_THRESHOLD = -1.0

# Safe default when we cannot understand the Omnigent event at all: ASK. A
# governance layer that cannot assess an action must surface it, never
# silently allow it.
_SAFE_DEFAULT = {
    "result": "ASK",
    "reason": "Hedwig could not assess this action; surfacing for review.",
}


def _trust_db_path() -> Path:
    """Locate the Hedwig trust.db for this repo.

    Honors HEDWIG_TRUST_DB (explicit override, used by tests and by an
    operator pointing the policy at a specific store) and otherwise falls back
    to the conventional per-repo `.sc/trust.db`.
    """
    override = os.environ.get("HEDWIG_TRUST_DB")
    if override:
        return Path(override)
    return Path.cwd() / ".sc" / "trust.db"


def _extract_action(event: Any) -> tuple[Path, str, str, str, bool, int] | None:
    """Pull (repo_root, rel_path, old_content, new_content, is_new_file,
    diff_size) out of an Omnigent tool_call event.

    *** OMNIGENT-FACING SHIM — VERIFY AGAINST REAL OMNIGENT API. ***
    This is the ONLY function that reads Omnigent's event shape on the way in.
    If their event schema differs from what README documents, this is the one
    place to adjust. Returns None to signal "abstain / can't assess", which the
    caller maps to the safe ASK default (or to abstain on non-tool phases).
    """
    if not isinstance(event, dict):
        return None
    if event.get("type") != "tool_call":
        # Hedwig only governs file actions; abstain on non-tool phases.
        return None

    data = event.get("data")
    if not isinstance(data, dict):
        return None
    args = data.get("arguments")
    if not isinstance(args, dict):
        return None

    # File path: Omnigent tools use `path`; Claude-native tools use `file_path`
    # (this dual-key fallback is exactly what Omnigent's own builtin file
    # policy does — see README source citation).
    rel = args.get("path") or args.get("file_path")
    if not rel or not isinstance(rel, str):
        return None

    # Repo root: prefer the event context's cwd, else the process cwd.
    # blast_radius scanning is rooted here.
    context = event.get("context")
    cwd = None
    if isinstance(context, dict):
        cwd = context.get("cwd") or context.get("repo_root")
    repo_root = Path(cwd) if cwd else Path.cwd()

    # Old/new content. Edit-style tools carry old_string/new_string; write-style
    # tools carry content. We tolerate any subset — diff_size and is_new_file
    # degrade conservatively when content is absent.
    old_content = args.get("old_string") or ""
    new_content = (
        args.get("new_string")
        or args.get("content")
        or args.get("new_str")
        or ""
    )

    abs_path = repo_root / rel
    is_new_file = not abs_path.exists()

    # diff_size: line delta if we have content, else a conservative estimate
    # from the new content length so a large blind write still reads as large.
    if not old_content and new_content:
        diff_size = new_content.count("\n") + 1
    elif old_content or new_content:
        diff_size = abs(new_content.count("\n") - old_content.count("\n")) + 1
    else:
        diff_size = 1

    return repo_root, rel, old_content, new_content, is_new_file, diff_size


def _response(action: str, reason: str) -> dict[str, str]:
    """Build an Omnigent PolicyResponse dict from a Hedwig verdict + reason.

    *** OMNIGENT-FACING SHIM — VERIFY AGAINST REAL OMNIGENT API. ***
    The only place that constructs Omnigent's return shape. README documents
    the expected `{"result": ..., "reason": ...}` contract.
    """
    return {"result": _VERDICT.get(action, "ASK"), "reason": reason}


def _plain_reason(verdict: str, rel: str, history) -> str:
    """Plain-English reason string (no debug score= tokens — G4 discipline).

    Cites the per-file outcome history when it drove a tightening, so the
    history-driven nature is legible inside Omnigent's UI without overclaiming
    a trained classifier.
    """
    if verdict == "ASK":
        if history.denials > 0:
            return (
                f"{rel}: surfacing for review — Hedwig got more cautious here "
                f"after a recorded reversal/denial on this file."
            )
        return f"{rel}: surfacing for review based on this action's risk."
    return f"{rel}: auto-approving — low risk and clean outcome history."


def decide(event: Any) -> dict[str, str] | None:
    """Omnigent custom Python policy entry point.

    Signature matches Omnigent's documented contract:
        def policy(event: PolicyEvent) -> PolicyResponse | None

    Register in Omnigent YAML as:
        policies:
          hedwig_learned:
            type: function
            handler: integrations.omnigent.policy.decide

    Returns an ALLOW/ASK/DENY dict, or `None` to abstain on phases Hedwig does
    not govern. Any failure degrades to ASK (the safe default) — a governance
    policy never crashes the harness and never silently allows.
    """
    try:
        extracted = _extract_action(event)
    except Exception:
        return _SAFE_DEFAULT

    if extracted is None:
        # Non-tool phase or unparseable file action. Abstain on clearly
        # non-tool phases (Omnigent then consults other policies / its
        # default); surface anything that looked like a tool_call we couldn't
        # read as ASK.
        if isinstance(event, dict) and event.get("type") not in (None, "tool_call"):
            return None
        return _SAFE_DEFAULT

    repo_root, rel, old_content, new_content, is_new_file, diff_size = extracted

    try:
        risk = assess_risk(
            repo_root=repo_root,
            file_path=rel,
            old_content=old_content,
            new_content=new_content,
            is_new_file=is_new_file,
            diff_size=diff_size,
        )

        db = TrustDB(_trust_db_path())
        repo_root_str = str(repo_root)
        # Per-file outcome history — THE thing that makes this decision adapt
        # rather than be a static rule.
        history = db.policy_history(repo_root_str, rel, stage="apply")
        recent = db.recent_denials(repo_root_str, "__omnigent__", stage="apply")

        pi = PolicyInput.from_signals(
            history,
            risk,
            recent_denials=recent,
            files_in_action=1,
        )
        decision = HeuristicScorer().decide(
            pi,
            proceed_threshold=_PROCEED_THRESHOLD,
            flag_threshold=_FLAG_THRESHOLD,
        )
    except Exception:
        # Any Hedwig-side failure (corrupt db, unexpected content) surfaces
        # rather than allowing. The harness keeps running.
        return _SAFE_DEFAULT

    verdict = _VERDICT.get(decision.action, "ASK")
    reason = _plain_reason(verdict, rel, history)
    return _response(decision.action, reason)
