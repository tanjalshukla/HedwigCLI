from __future__ import annotations

"""Rendering helpers for the apply-stage decision flow.

All Rich panel output and terminal prompts for the apply stage live here.
`apply_stage.py` owns the policy decisions; this module owns how those
decisions are presented to the developer. The split means the policy
pipeline is testable without mocking Rich, and the UI can be iterated
without touching approval logic.
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..trust_db import TrustDB

from rich import print

from ..features import RiskSignals
from ..policy import PolicyDecision, within_scope_budget
from ..trust_db import PolicyHistory
from .checkin_styling import dominant_pushback_type, render_adapted_checkin_context
from .diff_view import render_proposed_patch
from .pause_reason import synthesize_pause_reason
from .soft_checkin import render_soft_checkin, SoftCheckinOutcome
from .theme import PALETTE
from .ui import (
    _prompt_approval,
    _prompt_permanent,
    _render_auto_approve_summary,
    _render_file_list,
    _render_policy_snapshot,
    _summarize_autonomy_rationale,
    _user_friendly_reason,
)


_TASK_INTENT_PHRASE = {
    "debug": "debugging task",
    "refactor": "refactor task",
    "create": "new feature",
    "test": "writing tests",
    "understand": "exploration task",
    "other": "general task",
}

_PERSONA_PHRASE = {
    "active": "working alongside you",
    "delegating": "letting Hedwig drive",
    "unknown": "calibrating your style",
}


def render_session_read_line(*, task_intent: str | None, persona: str | None) -> None:
    intent_text = _TASK_INTENT_PHRASE.get((task_intent or "").lower())
    persona_text = _PERSONA_PHRASE.get((persona or "").lower())
    parts = [p for p in (intent_text, persona_text) if p]
    if not parts:
        return
    line = ", ".join(parts)
    print(f"  [{PALETTE['meta']}]Reading session as: {line}[/{PALETTE['meta']}]")


def render_cochange_lines(*, trust_db: TrustDB, repo_root: str, touched_files: list[str]) -> None:
    """Per planned file, dim line listing the top-2 historically co-changed files.
    Silent when there's no signal — never renders a placeholder."""
    try:
        from ..cochange import cochanged_files
    except Exception:
        return
    for path in touched_files:
        try:
            pairs = cochanged_files(trust_db, repo_root, path, min_count=2, limit=2)
        except Exception:
            continue
        if not pairs:
            continue
        rendered = ", ".join(f"{p} ({n})" for p, n in pairs)
        print(f"  [{PALETTE['meta']}]{path} — historically co-changes with: {rendered}[/{PALETTE['meta']}]")


def render_context_retrieved_line() -> None:
    try:
        from . import context_capture as _ctx
        last = _ctx.last()
    except Exception:
        return
    n_notes = len(last.logic_notes)
    n_guides = len(last.guidelines)
    n_fb = len(last.feedback)
    if (n_notes + n_guides + n_fb) == 0:
        return
    bits: list[str] = []
    if n_notes:
        bits.append(f"{n_notes} repo note{'s' if n_notes != 1 else ''}")
    if n_guides:
        bits.append(f"{n_guides} guideline{'s' if n_guides != 1 else ''}")
    if n_fb:
        bits.append(f"{n_fb} past correction{'s' if n_fb != 1 else ''}")
    print(f"  [{PALETTE['meta']}]Context retrieved: {', '.join(bits)}[/{PALETTE['meta']}]")


def render_apply_policy_snapshot(
    *,
    touched_files: list[str],
    histories: dict[str, PolicyHistory],
    policies: dict[str, PolicyDecision],
    prompt_required: bool,
    denied_apply: list[str],
    milestone_reasons: tuple[str, ...],
    apply_risk: dict[str, RiskSignals] | None = None,
    task_intent: str | None = None,
    persona: str | None = None,
    trust_db=None,
    repo_root: str | None = None,
) -> None:
    # When a diff panel will follow immediately, suppress the pre-diff file list —
    # it's redundant. Only force-show for hard-constraint denials and milestone gates
    # where no diff follows.
    force = bool(denied_apply) or bool(milestone_reasons)
    _render_policy_snapshot(
        stage="apply",
        files=touched_files,
        histories=histories,
        policies=policies,
        force=force,
    )
    render_session_read_line(task_intent=task_intent, persona=persona)
    render_context_retrieved_line()
    if trust_db is not None and repo_root:
        render_cochange_lines(trust_db=trust_db, repo_root=repo_root, touched_files=touched_files)
    if apply_risk:
        for path in touched_files:
            risk = apply_risk.get(path)
            if risk is None or not risk.model_risk_rationale:
                continue
            # Advisory adversarial-reviewer line. Style consistent with the
            # auto-approve summary's dim secondary rows.
            print(
                f"  [{PALETTE['meta']}]model reviewer  "
                f"{risk.model_risk_score:.2f} — "
                f"\"{risk.model_risk_rationale}\"[/{PALETTE['meta']}]"
            )


def render_apply_auto_approve_summary(
    *,
    touched_files: list[str],
    policies: dict[str, PolicyDecision],
    quantitative: str | None,
    qualitative: str | None,
    milestone_reasons: tuple[str, ...],
) -> None:
    _render_auto_approve_summary(
        "apply",
        quantitative,
        qualitative,
        _summarize_autonomy_rationale(
            files=touched_files,
            policies=policies,
            milestone_reasons=milestone_reasons,
        ),
    )


def render_hard_constraint_deny(denied_files: list[str]) -> None:
    from .theme import moment
    _style = moment("rule_hard")
    print(f"[{_style.title_style}]{_style.icon} change blocked by your rule[/{_style.title_style}]")
    _render_file_list(denied_files)


def render_apply_checkin_prompt(
    *,
    repo_root: Path,
    updates: dict[str, str],
    check_in_files: list[str],
    auto_files: list[str],
    apply_policies: dict[str, PolicyDecision],
    apply_risk: dict[str, RiskSignals],
    session_row_dicts: list[dict],
    verification_failure_rates: dict[str, float | None],
    remember: bool,
    scope_budget_files: int,
) -> tuple[bool, bool, str | None, int | None]:
    """Render the full check-in prompt and return (approved, remembered, feedback, response_time_ms)."""
    import time

    render_proposed_patch(repo_root, updates, check_in_files)

    _trailing_pushback_types = [
        row["pushback_type"] for row in session_row_dicts
        if row.get("pushback_type")
    ]
    _dominant = dominant_pushback_type(_trailing_pushback_types)
    _verification_hotspots = sorted(
        path for path, rate in verification_failure_rates.items()
        if rate is not None and rate >= 0.34
    )
    _max_blast = max(
        (r.blast_radius for r in apply_risk.values() if r),
        default=None,
    )
    render_adapted_checkin_context(
        dominant_type=_dominant,
        files=check_in_files,
        blast_radius=_max_blast,
        recent_verification_failures=_verification_hotspots,
    )

    allow_remember = remember and within_scope_budget(check_in_files, scope_budget_files)
    _pause_reason = synthesize_pause_reason(apply_policies, check_in_files)
    prompt_started = time.time()
    approved, remembered, feedback = _prompt_approval(
        "apply", check_in_files, allow_remember,
        pause_reason=_pause_reason,
        diff_already_shown=True,
    )
    response_time_ms = int((time.time() - prompt_started) * 1000)
    return approved, remembered, feedback, response_time_ms


def render_apply_denied(intervention: bool = False) -> None:
    if intervention:
        print(f"[{PALETTE['attention']}]✗ change rejected after you stopped it[/{PALETTE['attention']}]")
    else:
        print(f"[{PALETTE['attention']}]✗ patch denied[/{PALETTE['attention']}]")


def render_apply_auto_approved(
    *,
    all_leased: bool,
    flagged_auto_files: list[str],
    policies: dict[str, PolicyDecision],
    touched_files: list[str],
) -> str:
    """Render the auto-approve line and return the user_decision string."""
    _auto_why = _summarize_autonomy_rationale(files=touched_files, policies=policies)

    if all_leased:
        print(
            f"[{PALETTE['approve_bold']}]✓ apply approved[/{PALETTE['approve_bold']}]"
            f" [{PALETTE['meta']}]· reusing prior access[/{PALETTE['meta']}]"
        )
        return "auto_approve_lease"
    if flagged_auto_files:
        print(f"[{PALETTE['attention']}]✓ apply approved · flagged for review[/{PALETTE['attention']}]")
        _render_file_list(flagged_auto_files)
        return "auto_approve_flag"
    if _auto_why:
        print(
            f"[{PALETTE['approve_bold']}]✓ apply approved[/{PALETTE['approve_bold']}]"
            f"  [{PALETTE['meta']}]· {_auto_why}[/{PALETTE['meta']}]"
        )
    else:
        print(
            f"[{PALETTE['approve_bold']}]✓ apply approved[/{PALETTE['approve_bold']}]"
            f"  [{PALETTE['meta']}]· low risk, proceeding[/{PALETTE['meta']}]"
        )
    return "auto_approve"


def maybe_offer_permanent_lease(
    *,
    remember: bool,
    threshold: int,
    check_in_files: list[str],
    apply_constraints: dict,
    trust_db,
    repo_root_str: str,
    config,
) -> None:
    """Offer to promote frequently-approved files to permanent leases."""
    if not remember or threshold <= 0:
        return
    counts = trust_db.approved_apply_counts(repo_root_str, check_in_files)
    active_for_prompt = trust_db.active_leases(repo_root_str, check_in_files)
    eligible = [
        path for path in check_in_files
        if counts.get(path, 0) >= threshold
        and apply_constraints.get(path) is None
        and not (path in active_for_prompt and active_for_prompt[path].expires_at is None)
    ]
    if eligible and _prompt_permanent(eligible):
        trust_db.add_permanent_leases(repo_root_str, eligible, source="user_permanent")


def render_soft_checkin_gate(
    *,
    touched_files: list[str],
    apply_policies: dict[str, PolicyDecision],
) -> SoftCheckinOutcome:
    """Render soft-checkin panel and return outcome."""
    soft_reason = None
    for p in touched_files:
        rs = apply_policies.get(p)
        if rs:
            # Translate through the human-readable layer; raw scorer strings
            # like "scorer:0.41 -- 1.2 weighted approvals" aren't audience-readable.
            soft_reason = _user_friendly_reason(rs) or None
            if soft_reason:
                break
    return render_soft_checkin(
        stage="apply",
        files=touched_files,
        reason=soft_reason,
    )
