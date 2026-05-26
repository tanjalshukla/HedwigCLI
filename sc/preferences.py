from __future__ import annotations

"""Rich preference taxonomy for Hedwig.

The research contribution this module backs:

1. REPRESENTATION — a 5-dim schema (Trigger, Condition, PreferenceAction, Scope,
   Lifecycle) that captures developer preferences the existing
   AutonomyPreferences cannot express. Grounded in the SWE-chat analysis
   (docs/SWECHAT_ANALYSIS_REPORT.md).

2. INFERENCE — functions that read decision_traces and classify the developer's
   current session into session-level signals (coding mode, session intensity,
   pushback type). These are inferred, not developer-selected.

3. ACTIONABILITY — every Preference maps to a concrete PreferenceAction enum
   value. The threshold-adjustment path (autonomy.adjusted_policy_thresholds)
   reads Preferences and dispatches on action, so preferences are never just
   advisory.

Lives alongside sc/autonomy.py::AutonomyPreferences. Old and new coexist;
migration is gradual.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


# ---------------------------------------------------------------------------
# Session-level signals — grounded in the SWE-chat analysis.
# ---------------------------------------------------------------------------


class CodingMode(str, Enum):
    """Agent-authorship mode. Inferred per session from the ratio of
    agent-authored to human-authored code."""

    HUMAN_ONLY = "human_only"        # agent assists only; all code human-written
    COLLABORATIVE = "collaborative"  # 0 < agent-authored < 99%
    VIBE = "vibe"                    # >= 99% agent-authored


class UserPersona(str, Enum):
    """Session-level interaction style. Revised from the original 4-value
    enum based on behavioral clustering of 5,776 sessions — the data supports
    a 2-value intensity-based split, not persona-type labels."""

    DELEGATING = "delegating"  # short sessions, low pushback, high agent authorship
    ACTIVE = "active"          # long sessions, higher pushback, heavier human involvement
    UNKNOWN = "unknown"        # insufficient signal (low turn count)


class PushbackType(str, Enum):
    """Per-turn pushback category. Extended from the original 4-value enum
    based on topic analysis — 33% of real pushback messages didn't fit the
    original categories. Two new values characterize what was missing."""

    CORRECTION = "correction"               # user provides revised instructions
    REJECTION = "rejection"                 # user declines outright
    FAILURE_REPORT = "failure_report"       # user reports agent output caused error
    NON_PUSHBACK = "non_pushback"           # clean approval
    POSITIVE_REDIRECT = "positive_redirect"  # approval + new direction ("looks good, now do X")
    SCOPE_CONSTRAINT = "scope_constraint"   # narrowing ("just X, don't touch Y")


class TaskIntent(str, Enum):
    """Developer's current task intent, inferred from prompt text. Debug
    intent in particular is a strong pushback predictor."""

    DEBUG = "debug"
    REFACTOR = "refactor"
    CREATE = "create"
    TEST = "test"
    UNDERSTAND = "understand"
    OTHER = "other"


class TurnPurpose(str, Enum):
    """What a developer's turn is *doing*, separate from whether it's pushback.

    The v3 SWE-chat analysis of the unclassified 33% bucket revealed that
    most of it wasn't pushback at all — developers were providing context,
    issuing git commands, or pasting multi-part follow-up directives. This
    enum captures that orthogonal dimension: what the turn is *for*,
    regardless of its pushback category.

    Kept separate from PushbackType so Hedwig can react to "the developer
    is pasting an error log" (context_provision) differently from "the
    developer wants a correction" — even if both end up tagged as
    pushback in the raw trace.
    """

    CORRECTION_OR_DIRECTIVE = "correction_or_directive"  # standard instruction
    CONTEXT_PROVISION = "context_provision"              # logs, env, PRs, screenshots
    STRUCTURED_SPEC_INPUT = "structured_spec_input"      # todo lists, numbered specs
    SESSION_CONTINUATION = "session_continuation"        # "continue from where you left off"
    OTHER = "other"


# ---------------------------------------------------------------------------
# The 5-dim preference schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """What action pattern matches this preference.

    Predicate over RiskSignals + action context + task state. Unlike
    AutonomyPreferences' `allowed_checkin_topics` (OR-union only), Trigger
    can express AND/NOT via multiple fields that must all hold.
    """

    # RiskSignals-derived predicates. Kept for hard-constraint use cases
    # (security rules, never-touch paths); the analysis showed file-level
    # predicates are weak for inferred preferences.
    change_patterns: tuple[str, ...] = ()        # match if any listed
    min_blast_radius: int | None = None
    max_blast_radius: int | None = None
    min_diff_size: int | None = None
    max_diff_size: int | None = None
    requires_security_sensitive: bool | None = None  # True/False/None
    requires_new_file: bool | None = None

    # Stage predicate.
    stages: tuple[str, ...] = ()  # e.g. ("apply", "read")

    # Task-intent predicate. Added based on the SWE-chat analysis — debug
    # and refactor intent are stronger pushback predictors than file-level
    # features.
    task_intents: tuple[TaskIntent, ...] = ()  # match if current intent is any listed

    # Turn-purpose predicate. Used to distinguish "developer is pasting
    # context" from "developer is correcting" — context-provision turns
    # shouldn't drive oversight tightening.
    excludes_turn_purposes: tuple[str, ...] = ()  # values from TurnPurpose


@dataclass(frozen=True)
class Condition:
    """When this preference fires. Contextual, dynamic — evaluated against
    session state at decision time, not static action properties.
    """

    # Inferred session-level signals (None = don't care).
    required_coding_mode: CodingMode | None = None
    required_persona: UserPersona | None = None

    # Session-history predicates. These were the strongest pushback predictors
    # in the analysis.
    min_prior_pushback_count: int | None = None   # total pushback so far this session
    min_prior_failure_count: int | None = None    # failure reports so far this session
    max_recent_denials: int | None = None         # legacy; kept for back-compat
    min_recent_approvals: int | None = None       # legacy; kept for back-compat

    # Session-position predicate. Range 0..1 (0 = start, 1 = end).
    # Within-session behavior shifts significantly; this lets a preference
    # fire only late in a session, for example.
    session_position_min: float | None = None

    # Recent verification failures this session. 0 → don't care.
    # Used by the failure-signal trigger as a native-to-Hedwig signal
    # (traces log verification_passed directly).
    min_recent_verification_failures: int | None = None

    # Scorer-confidence predicate. Enables uncertainty-triggered check-ins.
    # Matches when |score - 0.5| <= max_uncertainty_band.
    max_uncertainty_band: float | None = None


class PreferenceAction(str, Enum):
    """What Hedwig does when trigger + condition both match. Fixed enum."""

    AUTO_APPLY = "auto_apply"             # proceed without prompting
    SOFT_CHECKIN = "soft_checkin"         # non-blocking panel; proceeds unless dev intervenes
    FULL_CHECKIN = "full_checkin"         # standard check-in with rationale


@dataclass(frozen=True)
class Scope:
    """Where this preference applies. Multi-level; checked outermost-first.

    Default for inferred preferences is ``session`` — the analysis showed
    cross-session behavior isn't stable (ICC 0.249), so inferred preferences
    shouldn't persist across sessions. Explicit developer preferences should
    override this and set level="repo" for project-wide preferences.
    """

    level: Literal["global", "repo", "session", "path"] = "session"
    # For level="path", the glob patterns; ignored otherwise.
    path_globs: tuple[str, ...] = ()
    # For level="session", the session_id it's bound to; ignored otherwise.
    session_id: str | None = None


@dataclass(frozen=True)
class Lifecycle:
    """Provenance + confidence. Together they make preferences auditable
    and revocable — the inverse of the old monotonic OR-union.

    Inferred preferences are bound to Scope(level="session"), so they
    naturally expire when the session ends; there is no separate half-life
    mechanism.
    """

    provenance: Literal[
        "user_explicit",           # developer set this directly
        "inferred",                # Hedwig learned it from traces
        "inferred_user_confirmed", # Hedwig learned it, developer confirmed
        "default",                 # built-in system preference
    ] = "inferred"
    confidence: float = 1.0                  # 0..1
    created_at: int = 0                      # unix ts


def default_lifecycle_for(
    provenance: str,
    *,
    created_at: int = 0,
) -> Lifecycle:
    """Build a Lifecycle for a preference. Scope-level bounds persistence,
    so no decay plumbing is needed here."""
    return Lifecycle(
        provenance=provenance,  # type: ignore[arg-type]
        created_at=created_at,
    )


@dataclass(frozen=True)
class Preference:
    """One preference record: 5 orthogonal dimensions.

    trigger + condition = WHEN this applies
    action            = WHAT Hedwig should do
    scope             = WHERE it applies
    lifecycle         = WHO/WHEN/HOW it was learned, and whether it's still valid
    """

    trigger: Trigger
    condition: Condition
    action: PreferenceAction
    scope: Scope
    lifecycle: Lifecycle = field(default_factory=Lifecycle)


# ---------------------------------------------------------------------------
# Built-in default preferences shipped with Hedwig.
# ---------------------------------------------------------------------------


FAILURE_SIGNAL_CHECKIN = Preference(
    # When the developer is in debug mode and there's already been a failure
    # in this session (either a developer-reported failure OR a verification
    # failure) — pause before the next write. Grounded in the SWE-chat
    # failure-report predictor (AUC 0.897) but mapped to signals Hedwig
    # actually captures from its own traces.
    trigger=Trigger(
        task_intents=(TaskIntent.DEBUG,),
        stages=("apply",),
    ),
    condition=Condition(
        # Either prior developer-reported failure OR recent verification
        # failure satisfies this trigger. The matcher evaluates them as OR,
        # not AND, since semantically both are "something went wrong lately."
        min_prior_failure_count=1,
    ),
    action=PreferenceAction.FULL_CHECKIN,
    scope=Scope(level="session"),
    lifecycle=Lifecycle(
        provenance="default",
        confidence=1.0,
    ),
)


DEFAULT_PREFERENCES: tuple[Preference, ...] = (
    FAILURE_SIGNAL_CHECKIN,
)


# ---------------------------------------------------------------------------
# Serialization — confirmed preferences round-trip through JSON in the
# confirmed_preferences table. Kept simple: one dict per field, enums as
# their .value strings.
# ---------------------------------------------------------------------------


def preference_to_dict(pref: Preference) -> dict[str, object]:
    """Serialize a Preference to a plain dict suitable for JSON encoding."""
    return {
        "trigger": {
            "change_patterns": list(pref.trigger.change_patterns),
            "min_blast_radius": pref.trigger.min_blast_radius,
            "max_blast_radius": pref.trigger.max_blast_radius,
            "min_diff_size": pref.trigger.min_diff_size,
            "max_diff_size": pref.trigger.max_diff_size,
            "requires_security_sensitive": pref.trigger.requires_security_sensitive,
            "requires_new_file": pref.trigger.requires_new_file,
            "stages": list(pref.trigger.stages),
            "task_intents": [ti.value for ti in pref.trigger.task_intents],
            "excludes_turn_purposes": list(pref.trigger.excludes_turn_purposes),
        },
        "condition": {
            "required_coding_mode": (
                pref.condition.required_coding_mode.value
                if pref.condition.required_coding_mode is not None
                else None
            ),
            "required_persona": (
                pref.condition.required_persona.value
                if pref.condition.required_persona is not None
                else None
            ),
            "min_prior_pushback_count": pref.condition.min_prior_pushback_count,
            "min_prior_failure_count": pref.condition.min_prior_failure_count,
            "max_recent_denials": pref.condition.max_recent_denials,
            "min_recent_approvals": pref.condition.min_recent_approvals,
            "session_position_min": pref.condition.session_position_min,
            "max_uncertainty_band": pref.condition.max_uncertainty_band,
            "min_recent_verification_failures": pref.condition.min_recent_verification_failures,
        },
        "action": pref.action.value,
        "scope": {
            "level": pref.scope.level,
            "path_globs": list(pref.scope.path_globs),
            "session_id": pref.scope.session_id,
        },
        "lifecycle": {
            "provenance": pref.lifecycle.provenance,
            "confidence": pref.lifecycle.confidence,
            "created_at": pref.lifecycle.created_at,
        },
    }


def preference_from_dict(data: dict[str, object]) -> Preference:
    """Deserialize a Preference from preference_to_dict output."""
    t = data.get("trigger", {})
    c = data.get("condition", {})
    s = data.get("scope", {})
    lc = data.get("lifecycle", {})
    return Preference(
        trigger=Trigger(
            change_patterns=tuple(t.get("change_patterns") or ()),
            min_blast_radius=t.get("min_blast_radius"),
            max_blast_radius=t.get("max_blast_radius"),
            min_diff_size=t.get("min_diff_size"),
            max_diff_size=t.get("max_diff_size"),
            requires_security_sensitive=t.get("requires_security_sensitive"),
            requires_new_file=t.get("requires_new_file"),
            stages=tuple(t.get("stages") or ()),
            task_intents=tuple(
                TaskIntent(v) for v in (t.get("task_intents") or ())
            ),
            excludes_turn_purposes=tuple(t.get("excludes_turn_purposes") or ()),
        ),
        condition=Condition(
            required_coding_mode=(
                CodingMode(c["required_coding_mode"])
                if c.get("required_coding_mode")
                else None
            ),
            required_persona=(
                UserPersona(c["required_persona"])
                if c.get("required_persona")
                else None
            ),
            min_prior_pushback_count=c.get("min_prior_pushback_count"),
            min_prior_failure_count=c.get("min_prior_failure_count"),
            max_recent_denials=c.get("max_recent_denials"),
            min_recent_approvals=c.get("min_recent_approvals"),
            session_position_min=c.get("session_position_min"),
            max_uncertainty_band=c.get("max_uncertainty_band"),
            min_recent_verification_failures=c.get("min_recent_verification_failures"),
        ),
        action=PreferenceAction(data.get("action", "full_checkin")),
        scope=Scope(
            level=s.get("level", "session"),
            path_globs=tuple(s.get("path_globs") or ()),
            session_id=s.get("session_id"),
        ),
        lifecycle=Lifecycle(
            provenance=lc.get("provenance", "default"),
            confidence=lc.get("confidence", 1.0),
            created_at=lc.get("created_at", 0),
        ),
    )


# ---------------------------------------------------------------------------
# Preference matching — evaluates Trigger + Condition against current state.
#
# Kept in this module because matching is the runtime expression of the schema
# defined above. Splitting it into a separate file meant callers needed two
# imports to do one thing: "does this preference fire right now?"
# ---------------------------------------------------------------------------

from fnmatch import fnmatch as _fnmatch


def match_failure_signal(
    *,
    session_summary: "SessionSummary",
    current_task_intent: TaskIntent,
    stage: str,
    recent_verification_failures: int = 0,
) -> "Preference | None":
    """Return FAILURE_SIGNAL_CHECKIN if its trigger + condition match, else None."""
    pref = FAILURE_SIGNAL_CHECKIN
    if pref.trigger.stages and stage not in pref.trigger.stages:
        return None
    if pref.trigger.task_intents and current_task_intent not in pref.trigger.task_intents:
        return None
    prior_failures = session_summary.n_failures + max(0, recent_verification_failures)
    if (
        pref.condition.min_prior_failure_count is not None
        and prior_failures < pref.condition.min_prior_failure_count
    ):
        return None
    return pref


def match_default_preferences(
    *,
    session_summary: "SessionSummary",
    current_task_intent: TaskIntent,
    stage: str,
    recent_verification_failures: int = 0,
) -> "tuple[Preference, ...]":
    """Return every built-in default Preference whose trigger + condition match."""
    matched: list[Preference] = []
    for pref in DEFAULT_PREFERENCES:
        if pref is FAILURE_SIGNAL_CHECKIN:
            if match_failure_signal(
                session_summary=session_summary,
                current_task_intent=current_task_intent,
                stage=stage,
                recent_verification_failures=recent_verification_failures,
            ) is not None:
                matched.append(pref)
    return tuple(matched)


def matches_preference(
    pref: Preference,
    *,
    risk: "RiskSignals | None",
    session_summary: "SessionSummary",
    current_task_intent: TaskIntent,
    stage: str,
    file_path: str | None,
    session_position: float | None,
    session_id: str | None,
    current_turn_purpose: str | None = None,
    recent_verification_failures: int = 0,
) -> bool:
    """Evaluate whether a Preference's trigger + condition + scope match this turn."""
    t = pref.trigger
    c = pref.condition

    if pref.scope.level == "session":
        if pref.scope.session_id is not None and pref.scope.session_id != session_id:
            return False
    if pref.scope.level == "path" and pref.scope.path_globs:
        if file_path is None:
            return False
        if not any(_fnmatch(file_path, pat) for pat in pref.scope.path_globs):
            return False

    if t.stages and stage not in t.stages:
        return False
    if t.task_intents and current_task_intent not in t.task_intents:
        return False
    if t.excludes_turn_purposes and current_turn_purpose in t.excludes_turn_purposes:
        return False

    if risk is not None:
        if t.change_patterns and risk.change_pattern not in t.change_patterns:
            return False
        if t.min_blast_radius is not None and risk.blast_radius < t.min_blast_radius:
            return False
        if t.max_blast_radius is not None and risk.blast_radius > t.max_blast_radius:
            return False
        if t.min_diff_size is not None and risk.diff_size < t.min_diff_size:
            return False
        if t.max_diff_size is not None and risk.diff_size > t.max_diff_size:
            return False
        if t.requires_security_sensitive is not None and risk.is_security_sensitive != t.requires_security_sensitive:
            return False
        if t.requires_new_file is not None and risk.is_new_file != t.requires_new_file:
            return False
    else:
        if (
            t.change_patterns or t.min_blast_radius is not None
            or t.max_blast_radius is not None or t.min_diff_size is not None
            or t.max_diff_size is not None or t.requires_security_sensitive is not None
            or t.requires_new_file is not None
        ):
            return False

    if c.min_prior_pushback_count is not None:
        # Count all meaningful pushback: denials, failures, and feedback turns.
        # Feedback includes scope constraints and corrections which are the most
        # common pushback types but weren't counted here before.
        total_pb = session_summary.n_denials + session_summary.n_failures + session_summary.n_feedback
        if total_pb < c.min_prior_pushback_count:
            return False
    if c.min_prior_failure_count is not None:
        effective_failures = session_summary.n_failures + max(0, recent_verification_failures)
        if effective_failures < c.min_prior_failure_count:
            return False
    if c.min_recent_verification_failures is not None and recent_verification_failures < c.min_recent_verification_failures:
        return False
    if c.max_recent_denials is not None and session_summary.n_denials > c.max_recent_denials:
        return False
    if c.min_recent_approvals is not None and session_summary.n_approvals < c.min_recent_approvals:
        return False
    if c.session_position_min is not None:
        if session_position is None or session_position < c.session_position_min:
            return False

    return True


def match_confirmed_preferences(
    confirmed: "tuple[Preference, ...]",
    *,
    risk: "RiskSignals | None",
    session_summary: "SessionSummary",
    current_task_intent: TaskIntent,
    stage: str,
    file_path: str | None,
    session_position: float | None,
    session_id: str | None,
    current_turn_purpose: str | None = None,
    recent_verification_failures: int = 0,
) -> "tuple[Preference, ...]":
    """Filter confirmed preferences to those matching this turn."""
    return tuple(
        p for p in confirmed
        if matches_preference(
            p,
            risk=risk,
            session_summary=session_summary,
            current_task_intent=current_task_intent,
            stage=stage,
            file_path=file_path,
            session_position=session_position,
            session_id=session_id,
            current_turn_purpose=current_turn_purpose,
            recent_verification_failures=recent_verification_failures,
        )
    )


def force_action_from_preferences(
    matched: "tuple[Preference, ...]",
) -> "PreferenceAction | None":
    """Return the most restrictive action across matched preferences, or None."""
    if not matched:
        return None
    strictness = {
        PreferenceAction.AUTO_APPLY: 1,
        PreferenceAction.SOFT_CHECKIN: 2,
        PreferenceAction.FULL_CHECKIN: 3,
    }
    return max((p.action for p in matched), key=lambda a: strictness.get(a, 0))


# Defer the SessionSummary / RiskSignals type annotations — they live in
# preference_inference.py and features.py respectively. Using string
# annotations above avoids a circular import since both those modules
# import from preferences.py.
try:
    from .preference_inference import SessionSummary  # noqa: F401 — re-export
    from .features import RiskSignals  # noqa: F401 — re-export
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Bridge: AutonomyPreferences (legacy coarse toggles) → Preference (5-dim).
# Lives here because the output schema is the 5-dim taxonomy. The legacy
# AutonomyPreferences type is imported only for typing.
# ---------------------------------------------------------------------------

# Topic → CHANGE_PATTERNS entries that carry the same semantic.
# "security" maps via requires_security_sensitive=True instead of a pattern.
_TOPIC_TO_CHANGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "api": ("api_change",),
    "schema": ("data_model_change",),
    "config": ("config_change",),
    "test": ("test_generation",),
    "security": (),
    "signature": (),
    "architecture": (),
    "deployment": (),
}


def autonomy_prefs_to_preferences(prefs) -> "tuple[Preference, ...]":
    """Convert an AutonomyPreferences into equivalent Preference objects.

    One-way bridge. AutonomyPreferences continues to drive the threshold-shift
    path; the returned Preferences feed the post-scorer override path so both
    systems contribute to force_action_from_preferences().

    Mapping:
    - prefer_fewer_checkins=True   → AUTO_APPLY (path-scoped if scoped_paths set,
                                     otherwise repo-scoped).
    - allowed_checkin_topics       → one FULL_CHECKIN per topic, repo-scoped.
                                     Trigger uses change_patterns, except
                                     "security" which uses requires_security_sensitive.
    - skip_low_risk_plan_checkpoint → not represented (plan-stage only).
    """
    lc = Lifecycle(provenance="inferred", confidence=1.0)
    result: list[Preference] = []

    if prefs.prefer_fewer_checkins:
        scope = (
            Scope(level="path", path_globs=prefs.scoped_paths)
            if prefs.scoped_paths
            else Scope(level="repo")
        )
        result.append(
            Preference(
                trigger=Trigger(stages=("apply",)),
                condition=Condition(),
                action=PreferenceAction.AUTO_APPLY,
                scope=scope,
                lifecycle=lc,
            )
        )

    for topic in prefs.allowed_checkin_topics:
        change_patterns = _TOPIC_TO_CHANGE_PATTERNS.get(topic, ())
        is_security = topic == "security"
        trigger = Trigger(
            change_patterns=change_patterns,
            requires_security_sensitive=True if is_security else None,
            stages=("apply",),
        )
        result.append(
            Preference(
                trigger=trigger,
                condition=Condition(),
                action=PreferenceAction.FULL_CHECKIN,
                scope=Scope(level="repo"),
                lifecycle=lc,
            )
        )

    return tuple(result)
