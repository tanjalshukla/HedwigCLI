from __future__ import annotations

"""Detect 'regret' events — cases where Hedwig auto-approved an action
and the developer later corrected, denied, or reported a failure on the
same file.

Regret is the honest counter-signal for autonomy: if the scorer is
proceeding too eagerly, regrets rise. The flow is:

1. Walk session traces in order.
2. Record each auto-approved file (user_decision startswith "auto_approve").
3. If a later turn on the same file is a denial, failure report, or a
   verification failure, count that as a regret attributable to the earlier
   auto-approve.

Grounded in Hedwig's trace schema — no extra instrumentation needed.
"""

from dataclasses import dataclass
from typing import Iterable


_AUTO_APPROVE_DECISIONS = {
    "auto_approve",
    "auto_approve_flag",
    "auto_approve_lease",
    "auto_approve_read_lease",
}

_REGRET_DECISIONS = {"deny", "interrupt"}


@dataclass(frozen=True)
class RegretEvent:
    """One auto-approved action that the developer later corrected or
    failed verification on."""

    file_path: str
    auto_approve_trace_id: int
    regret_trace_id: int
    reason: str  # "deny" | "interrupt" | "failure_report" | "verification_failed"


def detect_regret_events(
    rows: Iterable[dict],
) -> list[RegretEvent]:
    """Walk session trace rows in chronological order and return regrets.

    Expects each row to expose: id, file_path, user_decision,
    pushback_type, verification_passed.
    """
    auto_approved: dict[str, int] = {}  # file_path -> trace_id of auto-approve
    events: list[RegretEvent] = []

    for row in rows:
        file_path = row.get("file_path") or ""
        if not file_path:
            continue
        trace_id = int(row.get("id") or 0)
        decision = (row.get("user_decision") or "").lower()
        pushback = row.get("pushback_type") or ""
        verif = row.get("verification_passed")

        earlier = auto_approved.get(file_path)

        # Verification failure on a trace whose write was auto-approved counts
        # as a regret on that same trace. Check before the auto-approve branch
        # so self-attribution works.
        if (
            decision in _AUTO_APPROVE_DECISIONS
            and verif == 0
        ):
            events.append(
                RegretEvent(file_path, trace_id, trace_id, "verification_failed")
            )
            # Don't register this trace as a new auto-approve — it's already
            # been regretted.
            continue

        if decision in _AUTO_APPROVE_DECISIONS:
            auto_approved[file_path] = trace_id
            continue

        if earlier is None:
            continue

        if decision in _REGRET_DECISIONS:
            # Label with the actual decision ("deny" or "interrupt"), not a
            # hardcoded "deny" — so researcher-facing surfaces (/retrospective,
            # by_reason tallies, the HTML export) distinguish an explicit denial
            # from an interrupt. Both are corrective negative signal; the
            # gradient is identical, only the displayed cause differs.
            events.append(RegretEvent(file_path, earlier, trace_id, decision))
            auto_approved.pop(file_path, None)
            continue
        if pushback == "failure_report":
            events.append(
                RegretEvent(file_path, earlier, trace_id, "failure_report")
            )
            auto_approved.pop(file_path, None)
            continue
        if verif == 0:
            events.append(
                RegretEvent(file_path, earlier, trace_id, "verification_failed")
            )
            auto_approved.pop(file_path, None)

    return events


def regret_summary(rows: Iterable[dict]) -> dict:
    """Compact summary for CLI surfaces: count + breakdown by reason.

    Returns dict with keys: total, by_reason (dict), files (list[str]).
    """
    events = detect_regret_events(rows)
    by_reason: dict[str, int] = {}
    files: list[str] = []
    for e in events:
        by_reason[e.reason] = by_reason.get(e.reason, 0) + 1
        if e.file_path not in files:
            files.append(e.file_path)
    return {
        "total": len(events),
        "by_reason": by_reason,
        "files": files,
    }
