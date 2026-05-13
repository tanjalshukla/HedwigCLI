from __future__ import annotations

import unittest

from sc.ml_policy import build_cold_classifier
from sc.policy import (
    HeuristicScorer,
    PolicyInput,
    PolicyScorer,
    _heuristic_scorer,
    select_scorer,
)


def _pi() -> PolicyInput:
    return PolicyInput(
        prior_approvals=2.0,
        prior_denials=0,
        avg_response_ms=5000.0,
        avg_edit_distance=0.1,
        diff_size=20,
        blast_radius=2,
        is_new_file=False,
        is_security_sensitive=False,
        change_pattern="general_change",
        recent_denials=0,
        files_in_action=1,
    )


class PolicyScorerSeamTests(unittest.TestCase):
    def test_heuristic_satisfies_protocol(self) -> None:
        scorer: PolicyScorer = HeuristicScorer()
        self.assertTrue(scorer.ready())
        self.assertIsInstance(scorer.score(_pi()), float)

    def test_learned_satisfies_protocol(self) -> None:
        scorer: PolicyScorer = build_cold_classifier()
        # Cold classifier not ready until MIN_SAMPLES_FOR_LEARNED real decisions.
        self.assertFalse(scorer.ready())
        score = scorer.score(_pi())
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_select_scorer_picks_heuristic_when_no_classifier(self) -> None:
        scorer, label = select_scorer(classifier=None)
        self.assertIs(scorer, _heuristic_scorer)
        self.assertEqual(label, "heuristic")

    def test_select_scorer_picks_heuristic_when_classifier_cold(self) -> None:
        clf = build_cold_classifier()
        scorer, label = select_scorer(classifier=clf)
        self.assertIs(scorer, _heuristic_scorer)
        self.assertEqual(label, "heuristic")

    def test_select_scorer_picks_learned_when_ready(self) -> None:
        clf = build_cold_classifier()
        # Force-ready by bumping sample_count past the threshold. Production
        # gets there through real update() calls.
        from sc.ml_policy import MIN_SAMPLES_FOR_LEARNED
        clf.sample_count = MIN_SAMPLES_FOR_LEARNED
        scorer, label = select_scorer(classifier=clf)
        self.assertIs(scorer, clf)
        self.assertEqual(label, "learned")


if __name__ == "__main__":
    unittest.main()
