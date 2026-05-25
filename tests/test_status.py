from __future__ import annotations

import unittest

from sc.preference_inference import SessionSummary
from sc.status import (
    LearnedPreference,
    build_session_status,
    embellish_preference_basis,
    render_status_text,
    template_session_sentence,
    template_proactive_pause_sentence,
)


def _summary(
    n_turns: int = 7,
    n_approvals: int = 6,
    n_auto_approvals: int = 0,
) -> SessionSummary:
    return SessionSummary(
        session_id="s1",
        n_turns=n_turns,
        n_approvals=n_approvals,
        n_denials=0,
        n_feedback=0,
        n_failures=0,
        mean_edit_distance=0.1,
        mean_review_seconds=5.0,
        distinct_tasks=1,
        n_interruptions=0,
        n_auto_approvals=n_auto_approvals,
    )


class TemplateSentenceTests(unittest.TestCase):
    def test_empty_session_has_specific_sentence(self) -> None:
        status = build_session_status(summary=_summary(n_turns=0, n_approvals=0))
        sentence = template_session_sentence(status)
        self.assertIn("haven't exchanged", sentence)

    def test_delegating_session_reads_naturally(self) -> None:
        # High auto-approve rate + low intervention → delegating.
        # (4 auto-approves gives delegation_rate=1.0, intervention_rate=0.0)
        status = build_session_status(
            summary=_summary(n_turns=4, n_approvals=4, n_auto_approvals=4)
        )
        sentence = template_session_sentence(status)
        self.assertIn("delegating", sentence)
        self.assertIn("4 turns", sentence)

    def test_singular_turn_grammar(self) -> None:
        status = build_session_status(summary=_summary(n_turns=1, n_approvals=1))
        sentence = template_session_sentence(status)
        # Singular "1 turn" not "1 turns".
        self.assertIn("1 turn", sentence)
        self.assertNotIn("1 turns", sentence)


class ProactivePauseSentenceTests(unittest.TestCase):
    def test_zero_pauses_returns_none(self) -> None:
        status = build_session_status(summary=_summary())
        self.assertIsNone(template_proactive_pause_sentence(status))

    def test_one_pause_is_singular(self) -> None:
        status = build_session_status(
            summary=_summary(),
            most_recent_proactive_reason="a failure-signal pattern matched",
        )
        from dataclasses import replace
        status = replace(status, proactive_pauses=1)
        sentence = template_proactive_pause_sentence(status)
        self.assertIsNotNone(sentence)
        self.assertIn("paused you proactively once", sentence)

    def test_multiple_pauses_is_plural(self) -> None:
        status = build_session_status(
            summary=_summary(),
            most_recent_proactive_reason="a failure-signal pattern matched",
        )
        from dataclasses import replace
        status = replace(status, proactive_pauses=3)
        sentence = template_proactive_pause_sentence(status)
        assert sentence is not None
        self.assertIn("3 times", sentence)


class RenderStatusTests(unittest.TestCase):
    def test_empty_session_has_no_learning_line(self) -> None:
        status = build_session_status(summary=_summary(n_turns=0, n_approvals=0))
        lines = render_status_text(status)
        # Empty session should report that rather than claim nothing's been learned.
        self.assertTrue(any("haven't exchanged" in line for line in lines))

    def test_nonempty_session_without_preferences_acknowledges_that(self) -> None:
        status = build_session_status(summary=_summary())
        lines = render_status_text(status)
        text = "\n".join(lines)
        self.assertIn("haven't inferred any preferences", text)

    def test_confirmed_preference_renders(self) -> None:
        pref = LearnedPreference(
            headline="I'll check in before multi-file changes",
            basis="You narrowed scope on me 3 times.",
            scope="this session",
        )
        status = build_session_status(
            summary=_summary(),
            confirmed_session_preferences=(pref,),
        )
        lines = render_status_text(status)
        text = "\n".join(lines)
        self.assertIn("multi-file changes", text)
        self.assertIn("narrowed scope", text)


class LLMEmbellishmentTests(unittest.TestCase):
    def test_no_llm_returns_basis_verbatim(self) -> None:
        pref = LearnedPreference(
            headline="head",
            basis="original basis",
            scope="this session",
        )
        self.assertEqual(embellish_preference_basis(pref, llm_caller=None), "original basis")

    def test_llm_failure_falls_back_to_basis(self) -> None:
        pref = LearnedPreference(
            headline="head",
            basis="original basis",
            scope="this session",
        )

        def boom(_prompt: str) -> str:
            raise RuntimeError("bedrock down")

        result = embellish_preference_basis(pref, llm_caller=boom)
        self.assertEqual(result, "original basis")

    def test_empty_llm_response_falls_back(self) -> None:
        pref = LearnedPreference(
            headline="head",
            basis="original basis",
            scope="this session",
        )
        result = embellish_preference_basis(pref, llm_caller=lambda _p: "")
        self.assertEqual(result, "original basis")

    def test_suspiciously_long_llm_response_falls_back(self) -> None:
        pref = LearnedPreference(
            headline="head",
            basis="original basis",
            scope="this session",
        )
        result = embellish_preference_basis(pref, llm_caller=lambda _p: "x" * 500)
        self.assertEqual(result, "original basis")

    def test_good_llm_response_is_used(self) -> None:
        pref = LearnedPreference(
            headline="head",
            basis="original basis",
            scope="this session",
        )
        result = embellish_preference_basis(
            pref, llm_caller=lambda _p: "Better prose here."
        )
        self.assertEqual(result, "Better prose here.")


if __name__ == "__main__":
    unittest.main()
