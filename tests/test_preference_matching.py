from __future__ import annotations

import unittest

from sc.preference_inference import SessionSummary
from sc.preferences import (
    FAILURE_SIGNAL_CHECKIN,
    PreferenceAction,
    TaskIntent,
    force_action_from_preferences,
    match_default_preferences,
    match_failure_signal,
)


def _summary(n_failures: int = 0, n_turns: int = 5) -> SessionSummary:
    return SessionSummary(
        session_id="s1",
        n_turns=n_turns,
        n_approvals=n_turns,
        n_denials=0,
        n_feedback=0,
        n_failures=n_failures,
        mean_edit_distance=0.05,
        mean_prev_tools=2.0,
        mean_review_seconds=5.0,
        distinct_tasks=1,
        n_interruptions=0,
    )


class FailureSignalMatchTests(unittest.TestCase):
    def test_matches_when_debug_and_prior_failure(self) -> None:
        match = match_failure_signal(
            session_summary=_summary(n_failures=1),
            current_task_intent=TaskIntent.DEBUG,
            stage="apply",
        )
        self.assertIs(match, FAILURE_SIGNAL_CHECKIN)

    def test_matches_when_debug_and_recent_verification_failure(self) -> None:
        # The Hedwig-native version of the failure signal: if no developer
        # has reported a failure but a verification run failed, that's
        # still "something broke lately" — same behavior.
        match = match_failure_signal(
            session_summary=_summary(n_failures=0),
            current_task_intent=TaskIntent.DEBUG,
            stage="apply",
            recent_verification_failures=1,
        )
        self.assertIs(match, FAILURE_SIGNAL_CHECKIN)

    def test_no_match_when_not_debug(self) -> None:
        match = match_failure_signal(
            session_summary=_summary(n_failures=1),
            current_task_intent=TaskIntent.CREATE,
            stage="apply",
        )
        self.assertIsNone(match)

    def test_no_match_when_no_prior_failures(self) -> None:
        match = match_failure_signal(
            session_summary=_summary(n_failures=0),
            current_task_intent=TaskIntent.DEBUG,
            stage="apply",
        )
        self.assertIsNone(match)

    def test_no_match_wrong_stage(self) -> None:
        match = match_failure_signal(
            session_summary=_summary(n_failures=1),
            current_task_intent=TaskIntent.DEBUG,
            stage="read",
        )
        self.assertIsNone(match)


class MatchDefaultPreferencesTests(unittest.TestCase):
    def test_returns_failure_signal_when_pattern_matches(self) -> None:
        matched = match_default_preferences(
            session_summary=_summary(n_failures=1),
            current_task_intent=TaskIntent.DEBUG,
            stage="apply",
        )
        self.assertIn(FAILURE_SIGNAL_CHECKIN, matched)

    def test_returns_empty_when_no_pattern_matches(self) -> None:
        matched = match_default_preferences(
            session_summary=_summary(n_failures=0),
            current_task_intent=TaskIntent.CREATE,
            stage="apply",
        )
        self.assertEqual(matched, ())


class ForceActionTests(unittest.TestCase):
    def test_returns_none_when_nothing_matched(self) -> None:
        self.assertIsNone(force_action_from_preferences(()))

    def test_returns_full_checkin_when_failure_signal_matches(self) -> None:
        self.assertEqual(
            force_action_from_preferences((FAILURE_SIGNAL_CHECKIN,)),
            PreferenceAction.FULL_CHECKIN,
        )


class SerializationTests(unittest.TestCase):
    def test_preference_roundtrips_through_dict(self) -> None:
        from sc.preferences import (
            Condition,
            Lifecycle,
            Preference,
            PreferenceAction,
            Scope,
            TaskIntent,
            Trigger,
            UserPersona,
            preference_from_dict,
            preference_to_dict,
        )

        original = Preference(
            trigger=Trigger(
                task_intents=(TaskIntent.DEBUG,),
                stages=("apply",),
                excludes_turn_purposes=("context_provision",),
                min_blast_radius=3,
            ),
            condition=Condition(
                required_persona=UserPersona.ACTIVE,
                min_prior_pushback_count=2,
                session_position_min=0.33,
            ),
            action=PreferenceAction.FULL_CHECKIN,
            scope=Scope(level="session", session_id="abc"),
            lifecycle=Lifecycle(
                provenance="inferred_user_confirmed",
                confidence=0.9,
            ),
        )

        restored = preference_from_dict(preference_to_dict(original))
        self.assertEqual(restored.trigger.task_intents, original.trigger.task_intents)
        self.assertEqual(restored.trigger.min_blast_radius, 3)
        self.assertEqual(restored.condition.session_position_min, 0.33)
        self.assertEqual(restored.action, PreferenceAction.FULL_CHECKIN)
        self.assertEqual(restored.scope.level, "session")
        self.assertEqual(restored.scope.session_id, "abc")
        self.assertEqual(restored.lifecycle.provenance, "inferred_user_confirmed")


class MatchesPreferenceTests(unittest.TestCase):
    def _make_pref(self, **trigger_kwargs):
        from sc.preferences import (
            Condition,
            Lifecycle,
            Preference,
            PreferenceAction,
            Scope,
            Trigger,
        )
        return Preference(
            trigger=Trigger(**trigger_kwargs),
            condition=Condition(),
            action=PreferenceAction.FULL_CHECKIN,
            scope=Scope(level="repo"),
            lifecycle=Lifecycle(),
        )

    def _risk(self, **kwargs):
        from sc.features import RiskSignals
        defaults = dict(
            change_pattern="general_change",
            blast_radius=1,
            is_security_sensitive=False,
            is_new_file=False,
            diff_size=10,
        )
        defaults.update(kwargs)
        return RiskSignals(**defaults)

    def test_blast_radius_gate(self) -> None:
        from sc.preferences import matches_preference

        pref = self._make_pref(min_blast_radius=5, stages=("apply",))
        # Below threshold — no match.
        self.assertFalse(
            matches_preference(
                pref,
                risk=self._risk(blast_radius=2),
                session_summary=_summary(),
                current_task_intent=TaskIntent.OTHER,
                stage="apply",
                file_path="a.py",
                session_position=0.5,
                session_id="s1",
            )
        )
        # At or above — match.
        self.assertTrue(
            matches_preference(
                pref,
                risk=self._risk(blast_radius=5),
                session_summary=_summary(),
                current_task_intent=TaskIntent.OTHER,
                stage="apply",
                file_path="a.py",
                session_position=0.5,
                session_id="s1",
            )
        )

    def test_session_scope_requires_matching_id(self) -> None:
        from sc.preferences import matches_preference
        from sc.preferences import (
            Condition,
            Lifecycle,
            Preference,
            PreferenceAction,
            Scope,
            Trigger,
        )

        pref = Preference(
            trigger=Trigger(stages=("apply",)),
            condition=Condition(),
            action=PreferenceAction.FULL_CHECKIN,
            scope=Scope(level="session", session_id="s1"),
            lifecycle=Lifecycle(),
        )
        # Wrong session — no match.
        self.assertFalse(
            matches_preference(
                pref,
                risk=self._risk(),
                session_summary=_summary(),
                current_task_intent=TaskIntent.OTHER,
                stage="apply",
                file_path="a.py",
                session_position=0.5,
                session_id="s2",
            )
        )
        # Matching session.
        self.assertTrue(
            matches_preference(
                pref,
                risk=self._risk(),
                session_summary=_summary(),
                current_task_intent=TaskIntent.OTHER,
                stage="apply",
                file_path="a.py",
                session_position=0.5,
                session_id="s1",
            )
        )


if __name__ == "__main__":
    unittest.main()
