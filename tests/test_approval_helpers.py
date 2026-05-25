from __future__ import annotations

import unittest

from sc.run.helpers import _hard_constraint_decision, _lease_decision
from sc.trust_db import HardConstraint, Lease


class HardConstraintDecisionTests(unittest.TestCase):
    def _constraint(self, ctype: str) -> HardConstraint:
        return HardConstraint(
            path_pattern="**/*.py",
            source="test",
            overridable=False,
            constraint_type=ctype,
        )

    def test_always_deny_returns_deny_outcome(self) -> None:
        decision, label, outcome = _hard_constraint_decision(
            self._constraint("always_deny"), "write"
        )
        self.assertEqual(outcome, "deny")
        self.assertEqual(decision.action, "check_in")
        self.assertEqual(decision.score, -1000.0)
        self.assertEqual(label, "always_deny")

    def test_always_check_in_returns_check_in_outcome(self) -> None:
        decision, _, outcome = _hard_constraint_decision(
            self._constraint("always_check_in"), "read"
        )
        self.assertEqual(outcome, "check_in")
        self.assertEqual(decision.action, "check_in")
        self.assertEqual(decision.score, -500.0)

    def test_always_allow_returns_allow_outcome(self) -> None:
        decision, _, outcome = _hard_constraint_decision(
            self._constraint("always_allow"), "write"
        )
        self.assertEqual(outcome, "allow")
        self.assertEqual(decision.action, "proceed")
        self.assertEqual(decision.score, 900.0)

    def test_unknown_policy_passes_through(self) -> None:
        # Unrecognized constraint type must not crash; outcome is "passthrough"
        # so the caller falls into the lease/scorer tier.
        decision, _, outcome = _hard_constraint_decision(
            self._constraint("totally_made_up"), "write"
        )
        self.assertEqual(outcome, "passthrough")
        self.assertEqual(decision.score, 0.0)


class LeaseDecisionTests(unittest.TestCase):
    def _lease(self) -> Lease:
        return Lease(file_path="x.py", expires_at=None, lease_type="permanent")

    def test_read_lease_records_read_reason(self) -> None:
        decision = _lease_decision(self._lease(), "read")
        self.assertEqual(decision.action, "proceed")
        self.assertEqual(decision.score, 1000.0)
        self.assertEqual(decision.reasons, ("active read lease",))

    def test_write_lease_records_write_reason(self) -> None:
        decision = _lease_decision(self._lease(), "write")
        self.assertEqual(decision.reasons, ("active write lease",))


if __name__ == "__main__":
    unittest.main()
