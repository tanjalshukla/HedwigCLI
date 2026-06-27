from __future__ import annotations

"""Infer session-level signals from decision_traces.

The preference taxonomy only pays off if we can *infer* where a developer
sits without asking them. Every function here reads only from the trace
schema — no self-report, no static config.

Inference rules are heuristic priors, not learned. Their parameters are
grounded in the SWE-chat analysis (see docs/SWECHAT_ANALYSIS_REPORT.md)
and can be recalibrated from real Hedwig traces over time.
"""

import sqlite3
from dataclasses import dataclass
from typing import Sequence

from .preferences import CodingMode, PushbackType, TaskIntent, TurnPurpose, UserPersona
from .store.types import DecisionTraceRow


# Thresholds are documented priors, not tuned values. Rebalance from trace data.
# Coding-mode thresholds.
_VIBE_EDIT_DISTANCE_MAX = 0.05         # below: agent-authored uncorrected
_HUMAN_ONLY_APPROVAL_RATE_MAX = 0.50   # revised from 0.15 — the 0.15 value left
                                        # human-only nearly undetectable

# Session-intensity thresholds. Grounded in the SWE-chat Q3 cluster centers:
# "active" cluster averaged 24.9 turns; "delegating" averaged 7.6 turns.
# Turn count is the primary discriminator; tool-call rate is not recorded in
# Hedwig's schema (the agent's tool calls are opaque to the governance layer).
_ACTIVE_TURNS_MIN = 12                  # between the two cluster centers
_ACTIVE_TOOLS_PER_TURN_MIN = 6.0        # between the two cluster centers
_UNKNOWN_TURNS_MAX = 2                  # below: not enough signal yet

# Debug-intent keyword tokens (prompt text).
_DEBUG_TOKENS = (
    "debug", "bug", "error", "fix", "broken", "crash", "traceback",
    "failing", "fails", "not working", "doesn't work", "issue",
)
_REFACTOR_TOKENS = (
    "refactor", "clean up", "cleanup", "restructure", "rename",
    "reorganize", "simplify",
)
_TEST_TOKENS = (
    "test", "tests", "pytest", "unit test", "integration test",
)
_CREATE_TOKENS = (
    "add", "create", "implement", "build", "new endpoint", "new feature",
    "write", "introduce",
)
_UNDERSTAND_TOKENS = (
    "explain", "what does", "how does", "understand", "walk me through",
    "describe", "summarize",
)

# Pushback category phrase lists.
_FAILURE_PHRASES = (
    "failed", "broke", "broken", "crashed", "threw", "raises",
    "traceback", "doesn't work", "does not work", "didn't work",
    "not working", "this is wrong", "produced an error",
    "got an error", "raised exception",
)
_POSITIVE_REDIRECT_PHRASES = (
    "looks good", "great", "nice", "perfect", "i like it", "like it",
    "lgtm", "works", "now let", "now do", "next,", "next:", "then do",
    "moving on", "ok now", "good, now",
)
_SCOPE_CONSTRAINT_PHRASES = (
    "just do", "just the", "just focus", "just keep", "only do",
    "only the", "only focus", "don't touch", "don't modify",
    "don't change", "leave", "avoid", "skip", "limit to",
    "narrow", "narrow scope", "separately", "store first", "first then",
    "one at a time", "start with", "focus on", "scope to",
)


@dataclass(frozen=True)
class SessionSummary:
    """Compact summary of one session's trace history. Inputs to inference."""

    session_id: str
    n_turns: int
    n_approvals: int
    n_denials: int
    n_feedback: int              # traces with non-empty user_feedback_text
    n_failures: int              # traces classified as failure_report
    mean_edit_distance: float    # 0..1; how much dev rewrote agent output
    mean_review_seconds: float   # mean time dev spent before approving/denying
    distinct_tasks: int          # different task strings in this session
    n_interruptions: int         # user_decision indicating hard stop
    n_auto_approvals: int = 0    # turns Hedwig approved without asking
    # Anthropic (2026) shows delegation and intervention are orthogonal axes:
    # experienced developers auto-approve MORE but also interrupt MORE.
    # These two rates replace the single intensity classification as separate signals.

    @property
    def approval_rate(self) -> float:
        return self.n_approvals / self.n_turns if self.n_turns else 0.0

    @property
    def deny_rate(self) -> float:
        return self.n_denials / self.n_turns if self.n_turns else 0.0

    @property
    def feedback_rate(self) -> float:
        return self.n_feedback / self.n_turns if self.n_turns else 0.0

    @property
    def delegation_rate(self) -> float:
        """Fraction of turns Hedwig handled without asking. High = developer is delegating."""
        return self.n_auto_approvals / self.n_turns if self.n_turns else 0.0

    @property
    def intervention_rate(self) -> float:
        """Fraction of turns developer explicitly stopped or denied. High = developer is vigilant."""
        return (self.n_denials + self.n_interruptions) / self.n_turns if self.n_turns else 0.0


def summarize_session(rows: Sequence[sqlite3.Row] | Sequence[DecisionTraceRow]) -> SessionSummary:
    """Build a SessionSummary from decision_traces rows for one session.

    Turn-purpose awareness: turns classified as context_provision,
    structured_spec_input, or session_continuation are *not* counted as
    pushback or feedback signals, even if their pushback_type says so.
    A developer pasting an error log shouldn't raise Hedwig's watch-level —
    it's supplying context, not correcting. Grounded in the v3 SWE-chat
    finding that a third of "pushback" text wasn't actually pushback.

    Accepts sqlite3.Row or plain dicts (for testability). Rows must include:
    session_id, user_decision, edit_distance, user_feedback_text, task.
    Optional: pushback_type, prev_tool_count.
    """
    if not rows:
        return SessionSummary("", 0, 0, 0, 0, 0, 0.0, 0.0, 0, 0, 0)

    session_id = _get(rows[0], "session_id") or ""
    n_turns = len(rows)
    n_approvals = 0
    n_denials = 0
    n_feedback = 0
    n_failures = 0
    n_interruptions = 0
    n_auto_approvals = 0
    edit_sum = 0.0
    edit_count = 0
    review_sum = 0.0
    review_count = 0
    tasks: set[str] = set()

    # Turn purposes that represent supplying context, not pushing back.
    # Turns in these purposes bypass feedback/failure counting even if their
    # pushback_type label says otherwise.
    _context_like_purposes = {
        TurnPurpose.CONTEXT_PROVISION.value,
        TurnPurpose.STRUCTURED_SPEC_INPUT.value,
        TurnPurpose.SESSION_CONTINUATION.value,
    }

    _auto_approve_prefixes = ("auto_approve",)

    for row in rows:
        decision = (_get(row, "user_decision") or "").lower()
        if decision.startswith("approve"):
            n_approvals += 1
        if decision.startswith(_auto_approve_prefixes):
            n_auto_approvals += 1
        elif decision == "deny":
            n_denials += 1
        elif decision == "interrupt":
            n_interruptions += 1

        # Turn-purpose gating: context-like turns don't inflate pushback counts.
        turn_purpose = _get(row, "turn_purpose") or ""
        is_context_like = turn_purpose in _context_like_purposes

        fb = _get(row, "user_feedback_text")
        if fb and str(fb).strip() and not is_context_like:
            n_feedback += 1

        pbt = _get(row, "pushback_type")
        if pbt == PushbackType.FAILURE_REPORT.value and not is_context_like:
            n_failures += 1

        ed = _get(row, "edit_distance")
        if ed is not None:
            try:
                edit_sum += float(ed)
                edit_count += 1
            except (TypeError, ValueError):
                pass

        rt = _get(row, "response_time_ms")
        if rt is not None:
            try:
                rt_val = float(rt)
                if rt_val > 0:
                    review_sum += rt_val / 1000.0
                    review_count += 1
            except (TypeError, ValueError):
                pass

        task = _get(row, "task")
        if task:
            tasks.add(str(task))

    mean_edit = edit_sum / edit_count if edit_count else 0.0
    mean_review = review_sum / review_count if review_count else 0.0
    return SessionSummary(
        session_id=session_id,
        n_turns=n_turns,
        n_approvals=n_approvals,
        n_denials=n_denials,
        n_feedback=n_feedback,
        n_failures=n_failures,
        mean_edit_distance=mean_edit,
        mean_review_seconds=mean_review,
        distinct_tasks=len(tasks),
        n_interruptions=n_interruptions,
        n_auto_approvals=n_auto_approvals,
    )


def infer_coding_mode(summary: SessionSummary) -> CodingMode:
    """Coding mode inferred from trace signals.

    Uses edit_distance + approval rate as proxies for "who authored the
    surviving code." The human_only threshold was raised from 0.15 to 0.50
    because the low threshold left almost every session looking collaborative.
    """
    if summary.n_turns == 0:
        return CodingMode.COLLABORATIVE
    if summary.approval_rate < _HUMAN_ONLY_APPROVAL_RATE_MAX:
        return CodingMode.HUMAN_ONLY
    if summary.mean_edit_distance <= _VIBE_EDIT_DISTANCE_MAX and summary.approval_rate > 0.7:
        return CodingMode.VIBE
    return CodingMode.COLLABORATIVE


def infer_user_persona(summary: SessionSummary) -> UserPersona:
    """Session intensity inferred from trace signals.

    Revised from the 4-value persona enum based on behavioral clustering of
    5,776 sessions. Now uses two orthogonal axes per Anthropic (2026):
    delegation_rate (fraction auto-approved) and intervention_rate
    (fraction denied/interrupted). High delegation + high intervention is a
    valid experienced-developer state — they let routine things run but step
    in firmly when needed.

    - ACTIVE: high intervention rate OR long session with heavy tool use.
    - DELEGATING: high delegation rate and low intervention rate.
    - UNKNOWN: insufficient signal (very short session).
    """
    if summary.n_turns <= _UNKNOWN_TURNS_MAX:
        return UserPersona.UNKNOWN
    # High intervention — developer is vigilant regardless of delegation.
    if summary.intervention_rate >= 0.15:
        return UserPersona.ACTIVE
    # Long engaged session — turn count is the primary discriminator.
    # (mean_prev_tools not recorded in schema; turns alone is a clean signal
    # from the SWE-chat cluster analysis.)
    if summary.n_turns >= _ACTIVE_TURNS_MIN:
        return UserPersona.ACTIVE
    # High delegation, low intervention — true delegating mode.
    if summary.delegation_rate >= 0.5 and summary.intervention_rate < 0.05:
        return UserPersona.DELEGATING
    # Ambiguous mid-range session (3–11 turns, moderate engagement). Returning
    # DELEGATING here was wrong — it suppressed hypothesis surfacing for sessions
    # that are engaged but not yet long enough to be classified ACTIVE. UNKNOWN
    # preserves hypothesis seeding while deferring the intensity classification.
    return UserPersona.UNKNOWN


def infer_task_intent(prompt_text: str | None) -> TaskIntent:
    """Task intent inferred from the developer's prompt text.

    Debug intent in particular is a strong pushback predictor (coefficient
    0.52 in the SWE-chat analysis). Refactor intent is a weaker but
    independent signal (0.33).
    """
    if not prompt_text:
        return TaskIntent.OTHER
    text = prompt_text.lower()
    if any(tok in text for tok in _DEBUG_TOKENS):
        return TaskIntent.DEBUG
    if any(tok in text for tok in _REFACTOR_TOKENS):
        return TaskIntent.REFACTOR
    if any(tok in text for tok in _TEST_TOKENS):
        return TaskIntent.TEST
    if any(tok in text for tok in _UNDERSTAND_TOKENS):
        return TaskIntent.UNDERSTAND
    if any(tok in text for tok in _CREATE_TOKENS):
        return TaskIntent.CREATE
    return TaskIntent.OTHER


def infer_turn_purpose(prompt_text: str | None) -> TurnPurpose:
    """Classify a developer's turn by purpose (separate from PushbackType).

    Purpose is what the turn is *for*; pushback is how the turn relates to
    the agent's last action. The two dimensions are orthogonal.

    Kept lean: only purposes that actually change Hedwig's behavior are
    detected (context_provision, structured_spec_input, session_continuation).
    Everything else is CORRECTION_OR_DIRECTIVE or OTHER.
    """
    if not prompt_text:
        return TurnPurpose.OTHER
    text = prompt_text.strip().lower()
    if not text:
        return TurnPurpose.OTHER

    # Continuation — very short phrases asking the agent to resume.
    if text in ("continue", "continue.", "continue from where you left off",
                "keep going", "go on", "proceed"):
        return TurnPurpose.SESSION_CONTINUATION

    # Structured spec — heavy markdown structure (headers, tables).
    if ("##" in text or "|" in text) and len(text) > 200:
        return TurnPurpose.STRUCTURED_SPEC_INPUT

    # Context provision — pasted logs, tracebacks, env info.
    context_markers = (
        "traceback", "stacktrace", "here's the error", "here's the log",
        "here's the output", "see attached", "screenshot", "image.png",
    )
    if any(marker in text for marker in context_markers):
        return TurnPurpose.CONTEXT_PROVISION

    return TurnPurpose.CORRECTION_OR_DIRECTIVE


def classify_pushback(
    user_decision: str | None,
    edit_distance: float | None,
    user_feedback_text: str | None,
) -> PushbackType:
    """Per-turn pushback type. Extended from the original 4-value enum to
    include POSITIVE_REDIRECT and SCOPE_CONSTRAINT based on topic analysis.

    Ordering matters — failure_report and scope_constraint are checked before
    positive_redirect because a positive-redirect phrase ("looks good") can
    co-occur with scope-narrowing ("looks good, just focus on X").
    """
    decision = (user_decision or "").lower()
    fb = (user_feedback_text or "").strip()
    fb_low = fb.lower()
    ed = float(edit_distance) if edit_distance is not None else 0.0

    # Failure report: developer reporting that something broke.
    if fb and any(phrase in fb_low for phrase in _FAILURE_PHRASES):
        return PushbackType.FAILURE_REPORT

    # Scope constraint: narrowing instructions. Check before positive_redirect
    # because phrases can co-occur.
    if fb and any(phrase in fb_low for phrase in _SCOPE_CONSTRAINT_PHRASES):
        return PushbackType.SCOPE_CONSTRAINT

    # Positive redirect: approval combined with new direction.
    if fb and decision.startswith("approve") and any(
        phrase in fb_low for phrase in _POSITIVE_REDIRECT_PHRASES
    ):
        return PushbackType.POSITIVE_REDIRECT

    # Rejection: clean deny with no constructive feedback.
    if decision == "deny" and not fb:
        return PushbackType.REJECTION

    # Correction: any feedback or meaningful edits that isn't one of the above.
    if fb or ed > 0.10:
        return PushbackType.CORRECTION

    # Deny with some feedback that didn't match other categories.
    if decision == "deny":
        return PushbackType.REJECTION

    return PushbackType.NON_PUSHBACK


def session_position(current_turn_index: int, estimated_total: int) -> float:
    """Return fraction of session elapsed, clamped to [0, 1]. Used by
    Condition.session_position_min. When total is unknown (current session),
    caller can estimate from typical session length or pass a running
    estimate."""
    if estimated_total <= 0:
        return 0.0
    return min(max(current_turn_index / estimated_total, 0.0), 1.0)


# ---------------------------------------------------------------------------
# Internal: row accessor that handles both sqlite3.Row and dict.
# ---------------------------------------------------------------------------


def _get(row: sqlite3.Row | dict, key: str):
    try:
        return row[key]  # sqlite3.Row supports this
    except (IndexError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Hypothesis generation — reads SessionSummary, emits PreferenceHypothesis.
#
# Kept in this module because hypotheses are generated from exactly what
# inference produces (SessionSummary + pushback counts). Having them in a
# separate module required callers to import from three places to do one thing.
# ---------------------------------------------------------------------------

from collections import Counter
from dataclasses import dataclass as _dataclass

from .preferences import (
    Condition,
    Preference,
    PreferenceAction,
    Scope,
    Trigger,
    default_lifecycle_for,
)


MIN_TRACES_FOR_HYPOTHESIS = 5
MIN_PUSHBACK_COUNT = 3

_DELIBERATE_REVIEW_MEAN_SECONDS = 12.0
_DELIBERATE_REVIEW_MIN_APPROVALS = 3
_RAPID_APPROVER_MEAN_SECONDS = 3.0
_RAPID_APPROVER_MIN_APPROVALS = 4
_FAILURE_REACTIVE_MIN_FAILURES = 2


@_dataclass(frozen=True)
class PreferenceHypothesis:
    """A candidate preference Hedwig wants the developer to confirm."""
    prompt: str
    rationale: str
    proposed_preference: Preference
    driver: str


def hypothesize_from_session(
    session_summary: SessionSummary,
    pushback_counts: dict[str, int],
    *,
    inferred_persona: UserPersona | None = None,
    recent_verification_failures: int = 0,
    already_surfaced: set[str] | None = None,
) -> PreferenceHypothesis | None:
    """Return the highest-priority un-surfaced PreferenceHypothesis, or None.

    NOTE: in the main apply_stage flow, this function is NOT called directly.
    Instead, `hypothesis_bank.seed_candidates_from_session()` calls the
    individual `_scope_narrowing_hypothesis()` etc. generators and seeds the
    bank. This function is retained for tests and any caller that wants a
    single-shot hypothesis without the bank pipeline.

    Priority: scope_constraint → failure_reactive → deliberate_reviewer
              → rapid_approver → positive_redirect.

    already_surfaced: drivers already asked this session — skipped so each
    pattern is surfaced at most once but multiple patterns can fire.
    Intensity gate: DELEGATING sessions never surface hypotheses.
    """
    if session_summary.n_turns < MIN_TRACES_FOR_HYPOTHESIS:
        return None
    if inferred_persona == UserPersona.DELEGATING:
        return None

    skip = already_surfaced or set()

    scope_count = pushback_counts.get(PushbackType.SCOPE_CONSTRAINT.value, 0)
    if scope_count >= MIN_PUSHBACK_COUNT and "scope_constraint" not in skip:
        return _scope_narrowing_hypothesis(scope_count)

    total_failures = session_summary.n_failures + max(0, recent_verification_failures)
    if total_failures >= _FAILURE_REACTIVE_MIN_FAILURES and "failure_reactive" not in skip:
        return _failure_reactive_hypothesis(total_failures)

    if _is_deliberate_reviewer(session_summary) and "deliberate_reviewer" not in skip:
        return _deliberate_reviewer_hypothesis(session_summary.n_approvals)

    if _is_rapid_approver(session_summary) and "rapid_approver" not in skip:
        return _rapid_approver_hypothesis(session_summary.n_approvals)

    positive_count = pushback_counts.get(PushbackType.POSITIVE_REDIRECT.value, 0)
    if positive_count >= MIN_PUSHBACK_COUNT and "positive_redirect" not in skip:
        return _positive_redirect_hypothesis(positive_count)

    return None


def pushback_counts_from_rows(rows: Sequence[DecisionTraceRow]) -> dict[str, int]:
    """Compute pushback type counts from decision_traces rows."""
    counter: Counter[str] = Counter()
    for row in rows:
        pbt = row.get("pushback_type")
        if pbt:
            counter[pbt] += 1
    return dict(counter)


def _is_deliberate_reviewer(summary: SessionSummary) -> bool:
    if summary.n_approvals < _DELIBERATE_REVIEW_MIN_APPROVALS:
        return False
    if summary.mean_review_seconds < _DELIBERATE_REVIEW_MEAN_SECONDS:
        return False
    return summary.approval_rate > 0.6


def _is_rapid_approver(summary: SessionSummary) -> bool:
    if summary.n_approvals < _RAPID_APPROVER_MIN_APPROVALS:
        return False
    if summary.mean_review_seconds <= 0:
        return False
    return (
        summary.mean_review_seconds < _RAPID_APPROVER_MEAN_SECONDS
        and summary.approval_rate > 0.8
        and summary.n_feedback == 0
    )


def _scope_narrowing_hypothesis(count: int) -> PreferenceHypothesis:
    preference = Preference(
        # min_blast_radius=1 ensures this fires even on single-file changes in the
        # demo repo where api.py may only have one importer. The scope-narrowing
        # pattern is about *the developer's behavior* (they narrowed scope repeatedly),
        # not about blast radius of the current action.
        trigger=Trigger(stages=("apply",), min_blast_radius=1),
        condition=Condition(min_prior_pushback_count=2, session_position_min=0.33),
        action=PreferenceAction.FULL_CHECKIN,
        scope=Scope(level="repo"),
        lifecycle=default_lifecycle_for("inferred_user_confirmed"),
    )
    return PreferenceHypothesis(
        prompt="You've narrowed scope on me a few times — want me to always check in before multi-file changes?",
        rationale=f"{count} scope-narrowing messages detected. Applies to future sessions in this repo.",
        proposed_preference=preference,
        driver="scope_constraint",
    )


def _positive_redirect_hypothesis(count: int) -> PreferenceHypothesis:
    preference = Preference(
        trigger=Trigger(stages=("apply",), max_blast_radius=1, max_diff_size=30),
        condition=Condition(),
        action=PreferenceAction.SOFT_CHECKIN,
        scope=Scope(level="repo"),
        lifecycle=default_lifecycle_for("inferred_user_confirmed"),
    )
    return PreferenceHypothesis(
        prompt="You've been moving fast and accepting small follow-ups — want me to use non-blocking panels for small single-file changes?",
        rationale=f"{count} positive-redirect messages detected. Small changes surface without blocking.",
        proposed_preference=preference,
        driver="positive_redirect",
    )


def _failure_reactive_hypothesis(failure_count: int) -> PreferenceHypothesis:
    preference = Preference(
        trigger=Trigger(stages=("apply",), min_diff_size=20),
        condition=Condition(min_prior_failure_count=1),
        action=PreferenceAction.FULL_CHECKIN,
        scope=Scope(level="repo"),
        lifecycle=default_lifecycle_for("inferred_user_confirmed"),
    )
    return PreferenceHypothesis(
        prompt=f"We've hit {failure_count} failures this session — want me to check in on non-trivial changes when things are unstable?",
        rationale="Tightens oversight on larger edits while debugging. Persists across sessions in this repo.",
        proposed_preference=preference,
        driver="failure_reactive",
    )


def _deliberate_reviewer_hypothesis(approvals: int) -> PreferenceHypothesis:
    preference = Preference(
        trigger=Trigger(stages=("apply",), max_blast_radius=1, max_diff_size=40),
        condition=Condition(),
        action=PreferenceAction.SOFT_CHECKIN,
        scope=Scope(level="repo"),
        lifecycle=default_lifecycle_for("inferred_user_confirmed"),
    )
    return PreferenceHypothesis(
        prompt="You're reviewing carefully and making real edits — want me to soft-check-in on small diffs and save full prompts for bigger changes?",
        rationale=f"{approvals} careful approvals this session. Respects your review style across this repo.",
        proposed_preference=preference,
        driver="deliberate_reviewer",
    )


def _rapid_approver_hypothesis(approvals: int) -> PreferenceHypothesis:
    preference = Preference(
        trigger=Trigger(stages=("apply",), min_diff_size=40),
        condition=Condition(),
        action=PreferenceAction.FULL_CHECKIN,
        scope=Scope(level="repo"),
        lifecycle=default_lifecycle_for("inferred_user_confirmed"),
    )
    return PreferenceHypothesis(
        prompt="You've been approving quickly — want me to always check in on larger changes (40+ lines) so you stay in the loop?",
        rationale=f"{approvals} rapid approvals this session. Just the big changes, not the small ones.",
        proposed_preference=preference,
        driver="rapid_approver",
    )
