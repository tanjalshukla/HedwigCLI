from __future__ import annotations

import unittest

from sc.preference_inference import (
    classify_pushback,
    infer_coding_mode,
    infer_task_intent,
    infer_user_persona,
    summarize_session,
)
from sc.preferences import (
    DEFAULT_PREFERENCES,
    FAILURE_SIGNAL_CHECKIN,
    CodingMode,
    Condition,
    Lifecycle,
    Preference,
    PreferenceAction,
    PushbackType,
    Scope,
    TaskIntent,
    Trigger,
    UserPersona,
    default_lifecycle_for,
)


def _row(
    *,
    decision: str = "approve",
    edit_distance: float | None = 0.05,
    feedback: str | None = None,
    task: str = "t1",
    session_id: str = "s1",
    prev_tool_count: int | None = None,
    pushback_type: str | None = None,
) -> dict:
    return {
        "session_id": session_id,
        "user_decision": decision,
        "edit_distance": edit_distance,
        "user_feedback_text": feedback,
        "task": task,
        "prev_tool_count": prev_tool_count,
        "pushback_type": pushback_type,
    }


class PreferenceSchemaTests(unittest.TestCase):
    def test_preference_is_constructible(self) -> None:
        pref = Preference(
            trigger=Trigger(
                task_intents=(TaskIntent.DEBUG,),
                stages=("apply",),
                excludes_turn_purposes=("context_provision",),
            ),
            condition=Condition(
                required_persona=UserPersona.ACTIVE,
                min_prior_pushback_count=2,
            ),
            action=PreferenceAction.FULL_CHECKIN,
            scope=Scope(level="session"),
            lifecycle=Lifecycle(provenance="inferred", confidence=0.8),
        )
        self.assertEqual(pref.action, PreferenceAction.FULL_CHECKIN)
        self.assertEqual(pref.trigger.task_intents, (TaskIntent.DEBUG,))
        self.assertEqual(pref.condition.required_persona, UserPersona.ACTIVE)
        self.assertEqual(pref.scope.level, "session")

    def test_preference_is_frozen(self) -> None:
        pref = Preference(
            trigger=Trigger(),
            condition=Condition(),
            action=PreferenceAction.AUTO_APPLY,
            scope=Scope(),
        )
        with self.assertRaises(Exception):
            pref.action = PreferenceAction.FULL_CHECKIN  # type: ignore[misc]

    def test_scope_default_is_session(self) -> None:
        # Inferred preferences should default to session scope; cross-session
        # behavior stability is low (ICC 0.249) so repo-scoped inferences would
        # encode noise.
        self.assertEqual(Scope().level, "session")

    def test_default_lifecycle_carries_provenance(self) -> None:
        inferred = default_lifecycle_for("inferred", created_at=1000)
        self.assertEqual(inferred.provenance, "inferred")
        self.assertEqual(inferred.created_at, 1000)
        explicit = default_lifecycle_for("user_explicit", created_at=1000)
        self.assertEqual(explicit.provenance, "user_explicit")


class FailureSignalPreferenceTests(unittest.TestCase):
    def test_failure_signal_preference_is_exported(self) -> None:
        self.assertIn(FAILURE_SIGNAL_CHECKIN, DEFAULT_PREFERENCES)

    def test_failure_signal_triggers_on_debug_intent(self) -> None:
        # The trigger must include debug intent. We dropped the SWE-chat
        # "prev_bash_count" predicate because Hedwig's model doesn't run
        # shell; verification failures + prior failure reports are the
        # Hedwig-native equivalent.
        self.assertIn(TaskIntent.DEBUG, FAILURE_SIGNAL_CHECKIN.trigger.task_intents)

    def test_failure_signal_requires_prior_failure(self) -> None:
        self.assertEqual(
            FAILURE_SIGNAL_CHECKIN.condition.min_prior_failure_count, 1
        )

    def test_failure_signal_fires_full_checkin(self) -> None:
        self.assertEqual(FAILURE_SIGNAL_CHECKIN.action, PreferenceAction.FULL_CHECKIN)

    def test_failure_signal_is_session_scoped(self) -> None:
        self.assertEqual(FAILURE_SIGNAL_CHECKIN.scope.level, "session")


class SessionSummaryTests(unittest.TestCase):
    def test_empty_session(self) -> None:
        s = summarize_session([])
        self.assertEqual(s.n_turns, 0)
        self.assertEqual(s.approval_rate, 0.0)

    def test_counts_approvals_and_denials(self) -> None:
        rows = [
            _row(decision="approve"),
            _row(decision="approve"),
            _row(decision="deny", feedback="no"),
        ]
        s = summarize_session(rows)
        self.assertEqual(s.n_approvals, 2)
        self.assertEqual(s.n_denials, 1)

    def test_mean_prev_tools(self) -> None:
        rows = [
            _row(prev_tool_count=10),
            _row(prev_tool_count=0),
            _row(prev_tool_count=5),
        ]
        s = summarize_session(rows)
        self.assertAlmostEqual(s.mean_prev_tools, 5.0)

    def test_failure_count_from_pushback_type(self) -> None:
        rows = [
            _row(pushback_type="failure_report"),
            _row(pushback_type="correction"),
            _row(pushback_type="failure_report"),
        ]
        s = summarize_session(rows)
        self.assertEqual(s.n_failures, 2)


class CodingModeInferenceTests(unittest.TestCase):
    def test_vibe_on_high_approval_low_edits(self) -> None:
        rows = [_row(decision="approve", edit_distance=0.02) for _ in range(10)]
        self.assertEqual(infer_coding_mode(summarize_session(rows)), CodingMode.VIBE)

    def test_human_only_on_mostly_denials(self) -> None:
        rows = [_row(decision="deny", feedback="no") for _ in range(10)]
        self.assertEqual(infer_coding_mode(summarize_session(rows)), CodingMode.HUMAN_ONLY)

    def test_collaborative_default(self) -> None:
        rows = [_row(decision="approve", edit_distance=0.25) for _ in range(5)] + [
            _row(decision="deny", feedback="not this")
        ]
        self.assertEqual(infer_coding_mode(summarize_session(rows)), CodingMode.COLLABORATIVE)


class UserPersonaInferenceTests(unittest.TestCase):
    def test_unknown_on_low_turn_count(self) -> None:
        self.assertEqual(
            infer_user_persona(summarize_session([_row(), _row()])),
            UserPersona.UNKNOWN,
        )

    def test_active_on_long_session(self) -> None:
        rows = [_row(decision="approve") for _ in range(15)]
        self.assertEqual(
            infer_user_persona(summarize_session(rows)),
            UserPersona.ACTIVE,
        )

    def test_active_on_high_tool_use(self) -> None:
        rows = [_row(decision="approve", prev_tool_count=13) for _ in range(5)]
        self.assertEqual(
            infer_user_persona(summarize_session(rows)),
            UserPersona.ACTIVE,
        )

    def test_delegating_on_short_low_tool_session(self) -> None:
        rows = [_row(decision="approve", prev_tool_count=3) for _ in range(5)]
        self.assertEqual(
            infer_user_persona(summarize_session(rows)),
            UserPersona.DELEGATING,
        )


class TaskIntentInferenceTests(unittest.TestCase):
    def test_debug_intent_from_prompt_text(self) -> None:
        self.assertEqual(
            infer_task_intent("The tests are failing, can you debug it?"),
            TaskIntent.DEBUG,
        )

    def test_refactor_intent(self) -> None:
        self.assertEqual(
            infer_task_intent("Refactor this module to use dependency injection"),
            TaskIntent.REFACTOR,
        )

    def test_create_intent(self) -> None:
        self.assertEqual(
            infer_task_intent("Add a new endpoint for user settings"),
            TaskIntent.CREATE,
        )

    def test_understand_intent(self) -> None:
        self.assertEqual(
            infer_task_intent("Explain how the policy cascade works"),
            TaskIntent.UNDERSTAND,
        )

    def test_other_intent_for_empty(self) -> None:
        self.assertEqual(infer_task_intent(""), TaskIntent.OTHER)
        self.assertEqual(infer_task_intent(None), TaskIntent.OTHER)


class PushbackClassificationTests(unittest.TestCase):
    def test_failure_report_for_traceback(self) -> None:
        self.assertEqual(
            classify_pushback("approve", 0.0, "this raised a traceback"),
            PushbackType.FAILURE_REPORT,
        )

    def test_correction_for_design_guidance(self) -> None:
        # "error codes" contains "error" — must NOT be misread as failure report.
        self.assertEqual(
            classify_pushback("approve", 0.2, "use error codes instead"),
            PushbackType.CORRECTION,
        )

    def test_rejection_for_clean_deny(self) -> None:
        self.assertEqual(
            classify_pushback("deny", None, None),
            PushbackType.REJECTION,
        )

    def test_non_pushback_for_silent_approve(self) -> None:
        self.assertEqual(
            classify_pushback("approve", 0.02, None),
            PushbackType.NON_PUSHBACK,
        )

    def test_correction_for_high_edit_distance_no_feedback(self) -> None:
        self.assertEqual(
            classify_pushback("approve", 0.30, None),
            PushbackType.CORRECTION,
        )

    def test_positive_redirect(self) -> None:
        self.assertEqual(
            classify_pushback("approve", 0.05, "looks good, now let's add caching"),
            PushbackType.POSITIVE_REDIRECT,
        )

    def test_scope_constraint(self) -> None:
        self.assertEqual(
            classify_pushback("approve", 0.05, "just do the API changes, don't touch the tests"),
            PushbackType.SCOPE_CONSTRAINT,
        )

    def test_scope_constraint_takes_priority_over_positive_redirect(self) -> None:
        # "looks good" + "just focus on X" → scope_constraint (narrowing wins).
        self.assertEqual(
            classify_pushback("approve", 0.05, "looks good, just focus on the backend"),
            PushbackType.SCOPE_CONSTRAINT,
        )


if __name__ == "__main__":
    unittest.main()
