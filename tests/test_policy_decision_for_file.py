from __future__ import annotations

import unittest

from sc.features import RiskSignals
from sc.ml_policy import build_cold_classifier
from sc.run.helpers import _policy_decision_for_file
from sc.trust_db import PolicyHistory


def _history(approvals: float = 2.0, denials: int = 0) -> PolicyHistory:
    return PolicyHistory(
        approvals=int(approvals),
        denials=denials,
        effective_approvals=approvals,
        rubber_stamp_approvals=0,
        avg_response_ms=4000.0,
        avg_edit_distance=0.1,
    )


def _risk(
    *,
    pattern: str = "general_change",
    diff_size: int = 20,
    blast_radius: int = 1,
    is_new_file: bool = False,
    is_security_sensitive: bool = False,
) -> RiskSignals:
    return RiskSignals(
        change_pattern=pattern,
        blast_radius=blast_radius,
        is_security_sensitive=is_security_sensitive,
        is_new_file=is_new_file,
        diff_size=diff_size,
    )


class PolicyDecisionForFileTests(unittest.TestCase):
    def test_heuristic_path_when_no_classifier(self) -> None:
        decision = _policy_decision_for_file(
            history=_history(),
            risk=_risk(),
            recent_denials=0,
            files_in_action=1,
            proceed_threshold=0.5,
            flag_threshold=0.0,
            classifier=None,
        )
        # Heuristic path returns rich reasons, not the "learned-policy score" string.
        self.assertTrue(decision.reasons)
        self.assertFalse(
            any(r.startswith("learned-policy") for r in decision.reasons)
        )

    def test_cold_classifier_uses_heuristic(self) -> None:
        # A classifier that hasn't accumulated MIN_SAMPLES_FOR_LEARNED decisions
        # should still route through the heuristic via select_scorer.
        clf = build_cold_classifier()
        decision = _policy_decision_for_file(
            history=_history(),
            risk=_risk(),
            recent_denials=0,
            files_in_action=1,
            proceed_threshold=0.5,
            flag_threshold=0.0,
            classifier=clf,
        )
        self.assertFalse(
            any(r.startswith("learned-policy") for r in decision.reasons)
        )

    def test_learned_path_labels_reasons(self) -> None:
        # Patch select_scorer to force learned path.
        clf = build_cold_classifier()
        clf.sample_count = 100  # > MIN_SAMPLES_FOR_LEARNED
        decision = _policy_decision_for_file(
            history=_history(),
            risk=_risk(),
            recent_denials=0,
            files_in_action=1,
            proceed_threshold=0.9,  # force check_in regardless of score
            flag_threshold=0.5,
            classifier=clf,
        )
        self.assertTrue(
            any(r.startswith("learned-policy score") for r in decision.reasons),
            f"Expected learned-policy reason, got {decision.reasons}",
        )
        self.assertIn(decision.action, {"proceed", "proceed_flag", "check_in"})


if __name__ == "__main__":
    unittest.main()
