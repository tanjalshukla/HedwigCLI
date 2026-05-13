from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .preferences import Preference


_CHECKIN_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "api": ("api", "endpoint", "interface", "contract"),
    "signature": ("signature", "function signature", "method signature"),
    "schema": ("schema", "migration", "data model", "database schema", "db schema"),
    "security": ("security", "auth", "authorization", "credential", "secret"),
    "architecture": ("architecture", "architectural"),
    "config": ("config", "configuration", ".env", "settings"),
    "test": ("test", "tests", "pytest", "unit test"),
    "deployment": ("deploy", "deployment", "release", "rollout"),
}

def _normalize_scope_token(token: str) -> str | None:
    cleaned = token.strip().strip(".,:;()[]{}\"'")
    if "/" not in cleaned:
        return None
    norm = str(PurePosixPath(cleaned))
    if not norm or norm == "." or norm.startswith("../"):
        return None
    return norm


@dataclass(frozen=True)
class AutonomyPreferences:
    prefer_fewer_checkins: bool = False
    allowed_checkin_topics: tuple[str, ...] = ()
    skip_low_risk_plan_checkpoint: bool = False
    scoped_paths: tuple[str, ...] = ()

    def to_json(self) -> str:
        payload = {
            "prefer_fewer_checkins": self.prefer_fewer_checkins,
            "allowed_checkin_topics": list(self.allowed_checkin_topics),
            "skip_low_risk_plan_checkpoint": self.skip_low_risk_plan_checkpoint,
            "scoped_paths": list(self.scoped_paths),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | None) -> "AutonomyPreferences":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except Exception:
            return cls()
        topics = data.get("allowed_checkin_topics") or []
        normalized_topics = tuple(
            sorted(
                {
                    str(topic).strip().lower()
                    for topic in topics
                    if str(topic).strip().lower() in _CHECKIN_TOPIC_KEYWORDS
                }
            )
        )
        scoped_paths = data.get("scoped_paths") or []
        normalized_scopes = tuple(
            sorted(
                {
                    normalized
                    for item in scoped_paths
                    if (normalized := _normalize_scope_token(str(item))) is not None
                }
            )
        )
        return cls(
            prefer_fewer_checkins=bool(data.get("prefer_fewer_checkins", False)),
            allowed_checkin_topics=normalized_topics,
            skip_low_risk_plan_checkpoint=bool(data.get("skip_low_risk_plan_checkpoint", False)),
            scoped_paths=normalized_scopes,
        )

    def prompt_lines(self) -> list[str]:
        lines: list[str] = []
        if self.prefer_fewer_checkins:
            lines.append("Prefer autonomous execution for low-risk refactors.")
        if self.allowed_checkin_topics:
            topic_text = ", ".join(self.allowed_checkin_topics)
            lines.append(f"Check in only for: {topic_text}.")
        if self.skip_low_risk_plan_checkpoint:
            lines.append("Skip plan checkpoints for low-risk multi-file cleanups.")
        if self.scoped_paths:
            lines.append(f"Preference scope: {', '.join(self.scoped_paths)}.")
        return lines


def _scope_matches(file_path: str, scopes: tuple[str, ...]) -> bool:
    if not scopes:
        return True
    norm_path = str(PurePosixPath(file_path))
    for scope in scopes:
        if "*" in scope and fnmatch(norm_path, scope):
            return True
        if norm_path == scope:
            return True
        prefix = scope.rstrip("/")
        if prefix and norm_path.startswith(prefix + "/"):
            return True
    return False


def preferences_from_model_payload(payload: dict[str, object]) -> AutonomyPreferences:
    topics_raw = payload.get("allowed_checkin_topics")
    topics: tuple[str, ...] = ()
    if isinstance(topics_raw, list):
        topics = tuple(
            sorted(
                {
                    str(item).strip().lower()
                    for item in topics_raw
                    if str(item).strip().lower() in _CHECKIN_TOPIC_KEYWORDS
                }
            )
        )
    scopes_raw = payload.get("scoped_paths")
    scopes: tuple[str, ...] = ()
    if isinstance(scopes_raw, list):
        scopes = tuple(
            sorted(
                {
                    normalized
                    for item in scopes_raw
                    if (normalized := _normalize_scope_token(str(item))) is not None
                }
            )
        )
    return AutonomyPreferences(
        prefer_fewer_checkins=bool(payload.get("prefer_fewer_checkins", False)),
        allowed_checkin_topics=topics,
        skip_low_risk_plan_checkpoint=bool(payload.get("skip_low_risk_plan_checkpoint", False)),
        scoped_paths=scopes,
    )


def merge_preferences(
    current: AutonomyPreferences,
    inferred: AutonomyPreferences,
) -> tuple[AutonomyPreferences, list[str]]:
    # Additive merge: OR for booleans, UNION for collections.
    # This is intentionally monotonic so that a single interaction cannot
    # silently erase accumulated guidance. To explicitly walk back a preference,
    # use revoke_preferences() or `hw observe preferences-revoke`.
    combined_topics = tuple(sorted(set(current.allowed_checkin_topics) | set(inferred.allowed_checkin_topics)))
    combined_scopes = tuple(sorted(set(current.scoped_paths) | set(inferred.scoped_paths)))
    updated = AutonomyPreferences(
        prefer_fewer_checkins=current.prefer_fewer_checkins or inferred.prefer_fewer_checkins,
        allowed_checkin_topics=combined_topics,
        skip_low_risk_plan_checkpoint=(
            current.skip_low_risk_plan_checkpoint or inferred.skip_low_risk_plan_checkpoint
        ),
        scoped_paths=combined_scopes,
    )
    inferred_changes: list[str] = []
    if updated.prefer_fewer_checkins and not current.prefer_fewer_checkins:
        inferred_changes.append("prefer fewer low-risk check-ins")
    if updated.allowed_checkin_topics != current.allowed_checkin_topics and updated.allowed_checkin_topics:
        inferred_changes.append(f"check-in scope={','.join(updated.allowed_checkin_topics)}")
    if updated.skip_low_risk_plan_checkpoint and not current.skip_low_risk_plan_checkpoint:
        inferred_changes.append("skip low-risk plan checkpoints")
    if updated.scoped_paths != current.scoped_paths and updated.scoped_paths:
        inferred_changes.append(f"scope={','.join(updated.scoped_paths)}")
    return updated, inferred_changes


def revoke_preferences(
    current: AutonomyPreferences,
    *,
    topics: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    prefer_fewer_checkins: bool = False,
    skip_low_risk_plan_checkpoint: bool = False,
) -> tuple[AutonomyPreferences, list[str]]:
    """Remove specific preferences from the current state.

    This is the subtractive counterpart to merge_preferences().
    Pass the fields you want to revoke:
    - topics: check-in topics to remove from allowed_checkin_topics
    - paths: path scopes to remove from scoped_paths
    - prefer_fewer_checkins=True: reset that boolean to False
    - skip_low_risk_plan_checkpoint=True: reset that boolean to False

    Returns the updated preferences and a list of human-readable descriptions
    of what was revoked.
    """
    new_topics = tuple(
        t for t in current.allowed_checkin_topics if t not in set(topics)
    )
    new_paths = tuple(
        p for p in current.scoped_paths if p not in set(paths)
    )
    updated = AutonomyPreferences(
        prefer_fewer_checkins=False if prefer_fewer_checkins else current.prefer_fewer_checkins,
        allowed_checkin_topics=new_topics,
        skip_low_risk_plan_checkpoint=(
            False if skip_low_risk_plan_checkpoint else current.skip_low_risk_plan_checkpoint
        ),
        scoped_paths=new_paths,
    )
    revoked: list[str] = []
    if current.prefer_fewer_checkins and not updated.prefer_fewer_checkins:
        revoked.append("revoked: prefer-fewer-check-ins")
    removed_topics = set(current.allowed_checkin_topics) - set(updated.allowed_checkin_topics)
    if removed_topics:
        revoked.append(f"revoked check-in topics: {','.join(sorted(removed_topics))}")
    if current.skip_low_risk_plan_checkpoint and not updated.skip_low_risk_plan_checkpoint:
        revoked.append("revoked: skip-low-risk-plan-checkpoint")
    removed_paths = set(current.scoped_paths) - set(updated.scoped_paths)
    if removed_paths:
        revoked.append(f"revoked scoped paths: {','.join(sorted(removed_paths))}")
    return updated, revoked


def adjusted_policy_thresholds(
    proceed_threshold: float,
    flag_threshold: float,
    preferences: AutonomyPreferences,
    *,
    file_path: str | None = None,
    model_checkin_approval_rate: float | None = None,
    model_checkin_total: int = 0,
    session_intensity: str | None = None,
    coding_mode: str | None = None,
) -> tuple[float, float]:
    adjusted_proceed = proceed_threshold
    adjusted_flag = flag_threshold

    autonomy_applies = preferences.prefer_fewer_checkins and (
        file_path is None or _scope_matches(file_path, preferences.scoped_paths)
    )
    if autonomy_applies:
        delta = 0.25
        if preferences.allowed_checkin_topics:
            delta += 0.10
        adjusted_proceed -= delta
        adjusted_flag -= delta

    if model_checkin_total >= 5 and model_checkin_approval_rate is not None and model_checkin_approval_rate < 0.40:
        adjusted_proceed += 0.15
        adjusted_flag += 0.15

    # Session intensity modulation. Small delta so the scorer still dominates;
    # this is a trim, not an override.
    #   ACTIVE   → developer is deep in dialogue; tighten (more check-ins).
    #   DELEGATING → developer is in accept-and-move mode; loosen slightly so
    #              we don't break their rhythm on borderline auto-approve cases.
    if session_intensity == "active":
        adjusted_proceed += 0.08
        adjusted_flag += 0.08
    elif session_intensity == "delegating":
        adjusted_proceed -= 0.05
        adjusted_flag -= 0.05

    # Coding-mode modulation. Vibe-coding (near-zero edit distance, developer
    # has surrendered authorship to the agent) is the riskiest mode: mistakes
    # compound silently. Tighten slightly so Hedwig stays in the loop.
    # Human-only sessions (developer writes most code) benefit from lighter
    # overhead — loosen slightly to not get in the way.
    if coding_mode == "vibe":
        adjusted_proceed += 0.06
        adjusted_flag += 0.06
    elif coding_mode == "human_only":
        adjusted_proceed -= 0.04
        adjusted_flag -= 0.04

    adjusted_proceed = max(adjusted_proceed, -0.5)
    adjusted_flag = max(adjusted_flag, -0.5)
    if adjusted_flag > adjusted_proceed:
        adjusted_flag = adjusted_proceed
    return adjusted_proceed, adjusted_flag


# ---------------------------------------------------------------------------
# Map AutonomyPreferences → equivalent Preference objects.
# ---------------------------------------------------------------------------

# Topic → CHANGE_PATTERNS entries that carry the same semantic.
# "security" maps to no change_pattern but uses requires_security_sensitive=True.
# Topics without a tight change_pattern match use empty tuples (broad trigger).
_TOPIC_TO_CHANGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "api": ("api_change",),
    "schema": ("data_model_change",),
    "config": ("config_change",),
    "test": ("test_generation",),
    "security": (),          # handled via requires_security_sensitive=True
    "signature": (),         # no fine-grained change_pattern; broad trigger is fine
    "architecture": (),
    "deployment": (),
}


def autonomy_prefs_to_preferences(prefs: "AutonomyPreferences") -> "tuple[Preference, ...]":
    """Convert an AutonomyPreferences into equivalent Preference objects.

    This is a one-way, non-destructive bridge.  AutonomyPreferences continues
    to exist and drive the threshold-shift path; the returned Preferences feed
    the post-scorer override path in apply_stage so both systems contribute to
    the final forced-action decision.

    Mapping rules (see instructions in CLAUDE.md):
    - prefer_fewer_checkins=True  → AUTO_APPLY Preference, repo-scoped,
                                    optionally narrowed by scoped_paths.
    - allowed_checkin_topics      → one FULL_CHECKIN Preference per topic,
                                    repo-scoped, Trigger uses change_patterns
                                    (or requires_security_sensitive for "security").
    - scoped_paths                → narrows the AUTO_APPLY preference via
                                    scope=Scope(level="path", path_globs=...).
                                    If prefer_fewer_checkins is False, no
                                    AUTO_APPLY preference is emitted even if
                                    scoped_paths is non-empty.
    - skip_low_risk_plan_checkpoint → not represented here (plan-stage only).
    """
    from .preferences import (
        Lifecycle,
        Preference,
        PreferenceAction,
        Scope,
        Trigger,
        Condition,
    )

    result: list[Preference] = []
    lc = Lifecycle(provenance="inferred", confidence=1.0)

    # AUTO_APPLY: fires when prefer_fewer_checkins is set.
    if prefs.prefer_fewer_checkins:
        if prefs.scoped_paths:
            scope = Scope(level="path", path_globs=prefs.scoped_paths)
        else:
            scope = Scope(level="repo")
        result.append(
            Preference(
                trigger=Trigger(stages=("apply",)),
                condition=Condition(),
                action=PreferenceAction.AUTO_APPLY,
                scope=scope,
                lifecycle=lc,
            )
        )

    # FULL_CHECKIN per allowed_checkin_topic.
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
