from __future__ import annotations

import unittest

from sc.preference_inference import (
    MIN_PUSHBACK_COUNT,
    MIN_TRACES_FOR_HYPOTHESIS,
    SessionSummary,
    hypothesize_from_session,
    pushback_counts_from_rows,
)
from sc.preferences import PreferenceAction, PushbackType, UserPersona


def _summary(
    n_turns: int = 10,
    mean_review_seconds: float = 5.0,
    mean_edit_distance: float = 0.05,
    n_feedback: int = 0,
) -> SessionSummary:
    return SessionSummary(
        session_id="s1",
        n_turns=n_turns,
        n_approvals=n_turns,
        n_denials=0,
        n_feedback=n_feedback,
        n_failures=0,
        mean_edit_distance=mean_edit_distance,
        mean_prev_tools=2.0,
        mean_review_seconds=mean_review_seconds,
        distinct_tasks=1,
        n_interruptions=0,
    )


class HypothesisTests(unittest.TestCase):
    def test_no_hypothesis_when_session_too_short(self) -> None:
        h = hypothesize_from_session(
            _summary(n_turns=MIN_TRACES_FOR_HYPOTHESIS - 1),
            {PushbackType.SCOPE_CONSTRAINT.value: 10},
        )
        self.assertIsNone(h)

    def test_no_hypothesis_when_no_pattern_reaches_threshold(self) -> None:
        h = hypothesize_from_session(
            _summary(),
            {PushbackType.SCOPE_CONSTRAINT.value: MIN_PUSHBACK_COUNT - 1},
        )
        self.assertIsNone(h)

    def test_scope_hypothesis_fires(self) -> None:
        h = hypothesize_from_session(
            _summary(),
            {PushbackType.SCOPE_CONSTRAINT.value: MIN_PUSHBACK_COUNT},
        )
        self.assertIsNotNone(h)
        assert h is not None
        self.assertEqual(h.driver, "scope_constraint")
        self.assertEqual(h.proposed_preference.action, PreferenceAction.FULL_CHECKIN)
        self.assertEqual(h.proposed_preference.scope.level, "repo")

    def test_positive_redirect_hypothesis_fires(self) -> None:
        h = hypothesize_from_session(
            _summary(),
            {PushbackType.POSITIVE_REDIRECT.value: MIN_PUSHBACK_COUNT},
        )
        self.assertIsNotNone(h)
        assert h is not None
        self.assertEqual(h.driver, "positive_redirect")
        self.assertEqual(h.proposed_preference.action, PreferenceAction.SOFT_CHECKIN)

    def test_scope_wins_over_positive_redirect_when_both_match(self) -> None:
        h = hypothesize_from_session(
            _summary(),
            {
                PushbackType.SCOPE_CONSTRAINT.value: MIN_PUSHBACK_COUNT,
                PushbackType.POSITIVE_REDIRECT.value: MIN_PUSHBACK_COUNT + 5,
            },
        )
        assert h is not None
        self.assertEqual(h.driver, "scope_constraint")

    def test_confirmed_preference_provenance(self) -> None:
        h = hypothesize_from_session(
            _summary(),
            {PushbackType.SCOPE_CONSTRAINT.value: MIN_PUSHBACK_COUNT},
        )
        assert h is not None
        self.assertEqual(h.proposed_preference.lifecycle.provenance, "inferred_user_confirmed")

    def test_confirmed_preference_scope_is_repo(self) -> None:
        h = hypothesize_from_session(
            _summary(),
            {PushbackType.SCOPE_CONSTRAINT.value: MIN_PUSHBACK_COUNT},
        )
        assert h is not None
        self.assertEqual(h.proposed_preference.scope.level, "repo")


class IntensityAndReviewTimeTests(unittest.TestCase):
    def test_delegating_persona_suppresses_hypothesis(self) -> None:
        h = hypothesize_from_session(
            _summary(),
            {PushbackType.SCOPE_CONSTRAINT.value: MIN_PUSHBACK_COUNT},
            inferred_persona=UserPersona.DELEGATING,
        )
        self.assertIsNone(h)

    def test_deliberate_reviewer_fires_on_long_review_time(self) -> None:
        h = hypothesize_from_session(
            _summary(mean_review_seconds=20.0),
            {},
        )
        assert h is not None
        self.assertEqual(h.driver, "deliberate_reviewer")

    def test_rapid_approver_fires_on_short_review_time(self) -> None:
        h = hypothesize_from_session(
            _summary(mean_review_seconds=1.5, mean_edit_distance=0.0),
            {},
        )
        assert h is not None
        self.assertEqual(h.driver, "rapid_approver")

    def test_no_review_time_data_does_not_fire_rapid(self) -> None:
        h = hypothesize_from_session(
            _summary(mean_review_seconds=0.0, mean_edit_distance=0.0),
            {},
        )
        self.assertIsNone(h)

    def test_verification_failures_trigger_failure_reactive(self) -> None:
        h = hypothesize_from_session(
            _summary(),
            {},
            recent_verification_failures=2,
        )
        assert h is not None
        self.assertEqual(h.driver, "failure_reactive")


class PushbackCountsTests(unittest.TestCase):
    def test_counts_from_rows(self) -> None:
        rows = [
            {"pushback_type": PushbackType.CORRECTION.value},
            {"pushback_type": PushbackType.CORRECTION.value},
            {"pushback_type": PushbackType.POSITIVE_REDIRECT.value},
            {"pushback_type": None},
            {"pushback_type": PushbackType.SCOPE_CONSTRAINT.value},
        ]
        counts = pushback_counts_from_rows(rows)
        self.assertEqual(counts.get(PushbackType.CORRECTION.value), 2)
        self.assertEqual(counts.get(PushbackType.POSITIVE_REDIRECT.value), 1)
        self.assertEqual(counts.get(PushbackType.SCOPE_CONSTRAINT.value), 1)


if __name__ == "__main__":
    unittest.main()
