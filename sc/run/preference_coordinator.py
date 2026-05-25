from __future__ import annotations

"""Per-file preference resolution for the apply stage.

Wraps the three-source preference cascade — built-in defaults, AutonomyPreferences
derivations, and developer-confirmed preferences — and the asymmetric override
rules that combine a `PolicyDecision` with a matched `PreferenceAction`.

The coordinator is constructed once per turn (it captures session-wide state
in a `_SessionContext`) and queried per-file via `apply_to_decision`. Splitting
this out of `_evaluate_apply_stage` keeps the per-file scoring loop focused on
scoring and makes the override semantics independently testable.
"""

from dataclasses import dataclass

from ..features import RiskSignals
from ..policy import PolicyDecision
from ..preferences import (
    Preference,
    force_action_from_preferences,
    match_confirmed_preferences,
)


@dataclass(frozen=True)
class PreferenceMatch:
    """Result of evaluating preferences for one (file, decision) pair."""

    matched_confirmed: tuple[Preference, ...]
    matched_autonomy: tuple[Preference, ...]
    decision: PolicyDecision


class PreferenceCoordinator:
    """Resolves preference overrides on top of a scorer-produced PolicyDecision.

    Override semantics (preserved verbatim from prior inline logic):
      - ``full_checkin`` tightens ``proceed`` / ``proceed_flag`` to ``check_in``
        but never loosens an already-``check_in`` decision.
      - ``soft_checkin`` shifts a non-check_in to ``proceed_flag``.
      - ``auto_apply`` only fires when the scorer decided ``check_in`` —
        loosening it to ``proceed`` (this is how ``prefer_fewer_checkins``
        from ``AutonomyPreferences`` reaches the cascade).
      - Hard constraints (score == -1000 / -500) skip the scorer entirely
        and never reach this coordinator.
    """

    def __init__(
        self,
        *,
        confirmed_prefs: tuple[Preference, ...] | list[Preference],
        autonomy_derived_prefs: tuple[Preference, ...],
        matched_defaults: tuple[Preference, ...],
        session_summary,
        current_task_intent,
        current_turn_purpose: str,
        recent_verification_failures: int,
        session_position: float,
        session_id: str,
    ) -> None:
        self._confirmed_prefs: tuple[Preference, ...] = tuple(confirmed_prefs)
        self._autonomy_derived_prefs = autonomy_derived_prefs
        self._matched_defaults = matched_defaults
        self._session_summary = session_summary
        self._current_task_intent = current_task_intent
        self._current_turn_purpose = current_turn_purpose
        self._recent_verification_failures = recent_verification_failures
        self._session_position = session_position
        self._session_id = session_id

    def apply_to_decision(
        self,
        *,
        decision: PolicyDecision,
        file_path: str,
        risk: RiskSignals,
    ) -> PreferenceMatch:
        matched_confirmed = match_confirmed_preferences(
            self._confirmed_prefs,
            risk=risk,
            session_summary=self._session_summary,
            current_task_intent=self._current_task_intent,
            stage="apply",
            file_path=file_path,
            session_position=self._session_position,
            session_id=self._session_id,
            current_turn_purpose=self._current_turn_purpose,
            recent_verification_failures=self._recent_verification_failures,
        )
        matched_autonomy = match_confirmed_preferences(
            self._autonomy_derived_prefs,
            risk=risk,
            session_summary=self._session_summary,
            current_task_intent=self._current_task_intent,
            stage="apply",
            file_path=file_path,
            session_position=self._session_position,
            session_id=self._session_id,
            current_turn_purpose=self._current_turn_purpose,
            recent_verification_failures=self._recent_verification_failures,
        )
        all_matched = (
            self._matched_defaults
            + tuple(matched_confirmed)
            + tuple(matched_autonomy)
        )
        forced_action = force_action_from_preferences(all_matched)

        new_decision = self._apply_forced_action(
            decision=decision,
            forced_action=forced_action,
            matched_confirmed=matched_confirmed,
        )
        return PreferenceMatch(
            matched_confirmed=tuple(matched_confirmed),
            matched_autonomy=tuple(matched_autonomy),
            decision=new_decision,
        )

    @staticmethod
    def _apply_forced_action(
        *,
        decision: PolicyDecision,
        forced_action,
        matched_confirmed,
    ) -> PolicyDecision:
        if forced_action is None:
            return decision

        action_value = forced_action.value

        if action_value == "full_checkin" and decision.action != "check_in":
            from_confirmed = any(
                p.lifecycle.provenance == "inferred_user_confirmed"
                for p in matched_confirmed
                if p.action.value == "full_checkin"
            )
            reason = (
                "confirmed preference forced check-in"
                if from_confirmed
                else "failure-signal trigger: debug intent + prior failure this session"
            )
            return PolicyDecision(
                action="check_in",
                score=decision.score,
                reasons=decision.reasons + (reason,),
            )

        if action_value == "soft_checkin" and decision.action != "check_in":
            return PolicyDecision(
                action="proceed_flag",
                score=decision.score,
                reasons=decision.reasons + ("soft-checkin trigger matched",),
            )

        if action_value == "auto_apply" and decision.action == "check_in":
            return PolicyDecision(
                action="proceed",
                score=decision.score,
                reasons=decision.reasons + ("autonomy preference: proceed autonomously",),
            )

        return decision
