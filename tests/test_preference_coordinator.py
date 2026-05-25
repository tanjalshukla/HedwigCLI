from __future__ import annotations

import unittest

from sc.features import RiskSignals
from sc.policy import PolicyDecision
from sc.preference_inference import SessionSummary
from sc.preferences import (
    FAILURE_SIGNAL_CHECKIN,
    Condition,
    Lifecycle,
    Preference,
    PreferenceAction,
    Scope,
    TaskIntent,
    Trigger,
)
from sc.run.preference_coordinator import PreferenceCoordinator


def _summary(n_failures: int = 0) -> SessionSummary:
    return SessionSummary(
        session_id="s1",
        n_turns=5,
        n_approvals=5,
        n_denials=0,
        n_feedback=0,
        n_failures=n_failures,
        mean_edit_distance=0.05,
        mean_review_seconds=5.0,
        distinct_tasks=1,
        n_interruptions=0,
        n_auto_approvals=0,
    )


def _risk() -> RiskSignals:
    return RiskSignals(
        change_pattern="modify",
        blast_radius=1,
        is_security_sensitive=False,
        is_new_file=False,
        diff_size=10,
    )


def _coordinator(
    *,
    confirmed: tuple[Preference, ...] = (),
    autonomy: tuple[Preference, ...] = (),
    defaults: tuple[Preference, ...] = (),
    n_failures: int = 0,
) -> PreferenceCoordinator:
    return PreferenceCoordinator(
        confirmed_prefs=confirmed,
        autonomy_derived_prefs=autonomy,
        matched_defaults=defaults,
        session_summary=_summary(n_failures=n_failures),
        current_task_intent=TaskIntent.DEBUG,
        current_turn_purpose="implementation",
        recent_verification_failures=0,
        session_position=0.5,
        session_id="s1",
    )


class PreferenceCoordinatorTests(unittest.TestCase):
    def test_no_preferences_returns_decision_unchanged(self) -> None:
        coord = _coordinator()
        original = PolicyDecision(action="proceed", score=0.8, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision, original)

    def test_full_checkin_default_tightens_proceed_to_check_in(self) -> None:
        # FAILURE_SIGNAL_CHECKIN already pre-matched as a default; coordinator
        # forces full_checkin -> check_in with the default-source reason.
        coord = _coordinator(defaults=(FAILURE_SIGNAL_CHECKIN,), n_failures=1)
        original = PolicyDecision(action="proceed", score=0.8, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "check_in")
        self.assertIn(
            "failure-signal trigger: debug intent + prior failure this session",
            result.decision.reasons,
        )

    def test_full_checkin_does_not_loosen_existing_check_in(self) -> None:
        coord = _coordinator(defaults=(FAILURE_SIGNAL_CHECKIN,), n_failures=1)
        original = PolicyDecision(action="check_in", score=0.2, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "check_in")
        # No new reason appended when scorer already said check_in.
        self.assertEqual(result.decision.reasons, ("scorer",))

    def test_auto_apply_loosens_check_in_to_proceed(self) -> None:
        auto_pref = Preference(
            trigger=Trigger(stages=("apply",)),
            condition=Condition(),
            action=PreferenceAction.AUTO_APPLY,
            scope=Scope(level="repo"),
            lifecycle=Lifecycle(provenance="user_explicit", confidence=1.0),
        )
        coord = _coordinator(autonomy=(auto_pref,))
        original = PolicyDecision(action="check_in", score=0.4, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "proceed")
        self.assertIn(
            "autonomy preference: proceed autonomously",
            result.decision.reasons,
        )

    def test_soft_checkin_shifts_proceed_to_proceed_flag(self) -> None:
        soft_pref = Preference(
            trigger=Trigger(stages=("apply",)),
            condition=Condition(),
            action=PreferenceAction.SOFT_CHECKIN,
            scope=Scope(level="repo"),
            lifecycle=Lifecycle(provenance="user_explicit", confidence=1.0),
        )
        coord = _coordinator(autonomy=(soft_pref,))
        original = PolicyDecision(action="proceed", score=0.7, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "proceed_flag")
        self.assertIn("soft-checkin trigger matched", result.decision.reasons)

    def test_soft_checkin_does_not_loosen_check_in(self) -> None:
        soft_pref = Preference(
            trigger=Trigger(stages=("apply",)),
            condition=Condition(),
            action=PreferenceAction.SOFT_CHECKIN,
            scope=Scope(level="repo"),
            lifecycle=Lifecycle(provenance="user_explicit", confidence=1.0),
        )
        coord = _coordinator(autonomy=(soft_pref,))
        original = PolicyDecision(action="check_in", score=0.4, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "check_in")
        self.assertEqual(result.decision.reasons, ("scorer",))

    def test_full_checkin_wins_over_auto_apply(self) -> None:
        # When both AUTO_APPLY and FULL_CHECKIN match, FULL_CHECKIN must win
        # and only its reason is appended.
        auto_pref = Preference(
            trigger=Trigger(stages=("apply",)),
            condition=Condition(),
            action=PreferenceAction.AUTO_APPLY,
            scope=Scope(level="repo"),
            lifecycle=Lifecycle(provenance="user_explicit", confidence=1.0),
        )
        coord = _coordinator(
            autonomy=(auto_pref,),
            defaults=(FAILURE_SIGNAL_CHECKIN,),
            n_failures=1,
        )
        original = PolicyDecision(action="proceed", score=0.7, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "check_in")
        self.assertNotIn(
            "autonomy preference: proceed autonomously",
            result.decision.reasons,
        )


if __name__ == "__main__":
    unittest.main()
