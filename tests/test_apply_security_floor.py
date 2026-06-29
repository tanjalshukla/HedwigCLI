"""CLI security floor — a learned scorer that has drifted toward "approve
everything" must NOT be able to auto-apply a security-sensitive file.

Invariant 5 (the model is untrusted): assess_risk() flags is_security_sensitive
deterministically; that signal must survive as a FLOOR the learned
PolicyClassifier cannot override. The floor (apply_stage._apply_security_floor)
runs after the scorer/vote/preference layers and downgrades a security-sensitive
proceed -> check_in, mirroring the plugin floor in hedwig-decide.py.

This suite asserts the REAL floor function (not a mirror) plus the premise that
makes it load-bearing:
  1. PREMISE — a proceed-trained classifier genuinely returns `proceed` for a
     security-sensitive file via _policy_decision_for_file (the exact decision
     the floor must catch). If this stops being true the exploit window changed.
  2. FLOOR — _apply_security_floor downgrades that proceed to check_in, and
     leaves non-security and already-surfaced decisions untouched.
"""

from __future__ import annotations

import unittest

from sc.features import RiskSignals
from sc.ml_policy import build_cold_classifier
from sc.policy import PolicyDecision, PolicyInput
from sc.run.apply_stage import _apply_security_floor
from sc.run.helpers import _policy_decision_for_file
from sc.trust_db import PolicyHistory


def _approve_trained_classifier():
    """A learned classifier past MIN_SAMPLES_FOR_LEARNED, trained only on
    low-risk approvals — drifted toward 'approve everything', the state a busy
    session/booth produces."""
    clf = build_cold_classifier()
    pi = PolicyInput(
        prior_approvals=5, prior_denials=0, avg_response_ms=8000,
        avg_edit_distance=0.1, diff_size=8, blast_radius=1, is_new_file=False,
        is_security_sensitive=False, change_pattern="general_change",
        recent_denials=0, files_in_action=1,
    )
    for _ in range(14):
        clf.update(pi, approved=True)
    assert clf.ready()
    return clf


def _security_risk() -> RiskSignals:
    return RiskSignals(
        change_pattern="general_change", blast_radius=1,
        is_security_sensitive=True, is_new_file=False, diff_size=6,
    )


def _history() -> PolicyHistory:
    return PolicyHistory(
        approvals=5, denials=0, effective_approvals=5.0,
        rubber_stamp_approvals=0, avg_response_ms=8000.0, avg_edit_distance=0.1,
    )


class ApplySecurityFloorTests(unittest.TestCase):
    def test_premise_learned_scorer_proceeds_on_security_file(self) -> None:
        """The exploitable input is real: a drifted learned scorer returns
        proceed for a security-sensitive file at a learned-scale proceed bar
        (which adjusted_policy_thresholds can produce). If this regresses on its
        own, re-verify the floor still has coverage."""
        decision = _policy_decision_for_file(
            history=_history(),
            risk=_security_risk(),
            recent_denials=0,
            files_in_action=1,
            proceed_threshold=0.5,
            flag_threshold=0.25,
            classifier=_approve_trained_classifier(),
        )
        self.assertEqual(
            decision.action, "proceed",
            "premise changed: learned scorer no longer proceeds on a security "
            "file — the floor's exploit window must be re-verified",
        )

    def test_floor_downgrades_security_proceed_to_checkin(self) -> None:
        """The real floor converts a security-sensitive proceed to check_in."""
        floored = _apply_security_floor(
            PolicyDecision(action="proceed", score=1.0, reasons=("scorer:learned",)),
            _security_risk(),
        )
        self.assertEqual(floored.action, "check_in")
        self.assertTrue(any("security floor" in r for r in floored.reasons))

    def test_floor_chains_after_learned_decision_end_to_end(self) -> None:
        """Premise + floor together: the exact decision a drifted learned
        scorer produces for a security file, fed through the real floor, never
        stays proceed."""
        decision = _policy_decision_for_file(
            history=_history(), risk=_security_risk(), recent_denials=0,
            files_in_action=1, proceed_threshold=0.5, flag_threshold=0.25,
            classifier=_approve_trained_classifier(),
        )
        self.assertEqual(_apply_security_floor(decision, _security_risk()).action, "check_in")

    def test_floor_leaves_non_security_proceed_untouched(self) -> None:
        """A non-security proceed still auto-applies — the floor is narrow."""
        risk = RiskSignals(
            change_pattern="general_change", blast_radius=1,
            is_security_sensitive=False, is_new_file=False, diff_size=6,
        )
        proceed = PolicyDecision(action="proceed", score=1.0, reasons=("scorer:learned",))
        self.assertEqual(_apply_security_floor(proceed, risk).action, "proceed")

    def test_floor_leaves_surfaced_security_decision_untouched(self) -> None:
        """An already-surfaced (check_in) security decision is unchanged — the
        floor only tightens proceed, it never re-touches a verdict."""
        risk = _security_risk()
        check_in = PolicyDecision(action="check_in", score=-0.5, reasons=("x",))
        self.assertEqual(_apply_security_floor(check_in, risk).reasons, ("x",))


if __name__ == "__main__":
    unittest.main()
