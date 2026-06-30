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

    def test_auto_apply_does_not_loosen_check_in(self) -> None:
        # Invariant from CLAUDE.md: preferences add caution but never remove
        # it. AUTO_APPLY's effect on prefer_fewer_checkins is carried by the
        # threshold-shift path (adjusted_policy_thresholds), not by a
        # post-scorer override. A scorer-driven check_in must survive
        # regardless of any AUTO_APPLY preferences.
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
        self.assertEqual(result.decision.action, "check_in")
        self.assertEqual(result.decision.reasons, ("scorer",))

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

    # --- auto_apply override tests ---

    def _auto_apply_confirmed_pref(self, provenance: str = "user_explicit") -> Preference:
        return Preference(
            trigger=Trigger(stages=("apply",)),
            condition=Condition(),
            action=PreferenceAction.AUTO_APPLY,
            scope=Scope(level="repo"),
            lifecycle=Lifecycle(provenance=provenance, confidence=1.0),
        )

    def test_auto_apply_user_explicit_low_risk_loosens_check_in(self) -> None:
        # user_explicit provenance -> check_in becomes proceed unconditionally
        coord = _coordinator(confirmed=(self._auto_apply_confirmed_pref("user_explicit"),))
        original = PolicyDecision(action="check_in", score=0.4, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "proceed")
        self.assertIn(
            "auto_apply preference: user-explicit override",
            result.decision.reasons,
        )

    def test_auto_apply_inferred_user_confirmed_low_risk_loosens_check_in(self) -> None:
        # inferred_user_confirmed provenance + low risk -> check_in becomes proceed
        coord = _coordinator(confirmed=(self._auto_apply_confirmed_pref("inferred_user_confirmed"),))
        original = PolicyDecision(action="check_in", score=0.4, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "proceed")
        self.assertIn(
            "auto_apply preference: low-risk inferred-confirmed override",
            result.decision.reasons,
        )

    def test_auto_apply_user_explicit_large_diff_loosens_check_in(self) -> None:
        # user_explicit ignores diff_size guard — large diff still becomes proceed
        coord = _coordinator(confirmed=(self._auto_apply_confirmed_pref("user_explicit"),))
        original = PolicyDecision(action="check_in", score=0.4, reasons=("scorer",))
        high_diff_risk = RiskSignals(
            change_pattern="modify",
            blast_radius=1,
            is_security_sensitive=False,
            is_new_file=False,
            diff_size=25,
        )
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=high_diff_risk)
        self.assertEqual(result.decision.action, "proceed")
        self.assertIn("auto_apply preference: user-explicit override", result.decision.reasons)

    def test_auto_apply_user_explicit_security_sensitive_loosens_check_in(self) -> None:
        # user_explicit ignores is_security_sensitive guard at Step 7.
        # The Step 9 security floor enforces it independently; this test only
        # covers the preference_coordinator layer in isolation.
        coord = _coordinator(confirmed=(self._auto_apply_confirmed_pref("user_explicit"),))
        original = PolicyDecision(action="check_in", score=0.4, reasons=("scorer",))
        sec_risk = RiskSignals(
            change_pattern="modify",
            blast_radius=1,
            is_security_sensitive=True,
            is_new_file=False,
            diff_size=10,
        )
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=sec_risk)
        self.assertEqual(result.decision.action, "proceed")
        self.assertIn("auto_apply preference: user-explicit override", result.decision.reasons)

    def test_auto_apply_inferred_user_confirmed_large_diff_preserves_check_in(self) -> None:
        # inferred_user_confirmed still respects diff_size guard
        coord = _coordinator(confirmed=(self._auto_apply_confirmed_pref("inferred_user_confirmed"),))
        original = PolicyDecision(action="check_in", score=0.4, reasons=("scorer",))
        high_diff_risk = RiskSignals(
            change_pattern="modify",
            blast_radius=1,
            is_security_sensitive=False,
            is_new_file=False,
            diff_size=25,
        )
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=high_diff_risk)
        self.assertEqual(result.decision.action, "check_in")
        self.assertEqual(result.decision.reasons, ("scorer",))

    def test_auto_apply_default_provenance_preserves_check_in(self) -> None:
        # provenance="default" (built-in) never loosens, even with low risk
        coord = _coordinator(confirmed=(self._auto_apply_confirmed_pref("default"),))
        original = PolicyDecision(action="check_in", score=0.4, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        self.assertEqual(result.decision.action, "check_in")
        self.assertEqual(result.decision.reasons, ("scorer",))

    def test_full_checkin_beats_auto_apply_confirmed(self) -> None:
        # FULL_CHECKIN (from defaults) wins over auto_apply confirmed pref
        coord = _coordinator(
            confirmed=(self._auto_apply_confirmed_pref("user_explicit"),),
            defaults=(FAILURE_SIGNAL_CHECKIN,),
            n_failures=1,
        )
        original = PolicyDecision(action="proceed", score=0.7, reasons=("scorer",))
        result = coord.apply_to_decision(decision=original, file_path="x.py", risk=_risk())
        # force_action_from_preferences picks full_checkin (strictest), so
        # auto_apply branch never fires — forced_action is full_checkin.
        self.assertEqual(result.decision.action, "check_in")
        self.assertNotIn(
            "auto_apply preference: low-risk developer-confirmed override",
            result.decision.reasons,
        )


if __name__ == "__main__":
    unittest.main()
