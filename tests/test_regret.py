from __future__ import annotations

import unittest

from sc.regret import detect_regret_events, regret_summary


def _row(
    id_: int,
    file_path: str,
    user_decision: str = "",
    pushback_type: str = "",
    verification_passed: int | None = None,
) -> dict:
    return {
        "id": id_,
        "file_path": file_path,
        "user_decision": user_decision,
        "pushback_type": pushback_type,
        "verification_passed": verification_passed,
    }


class DetectRegretTests(unittest.TestCase):
    def test_no_regret_when_auto_approve_is_standalone(self) -> None:
        rows = [_row(1, "a.py", "auto_approve")]
        self.assertEqual(detect_regret_events(rows), [])

    def test_deny_after_auto_approve_is_regret(self) -> None:
        rows = [
            _row(1, "a.py", "auto_approve"),
            _row(2, "a.py", "deny"),
        ]
        events = detect_regret_events(rows)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, "deny")
        self.assertEqual(events[0].file_path, "a.py")

    def test_failure_report_after_auto_approve_is_regret(self) -> None:
        rows = [
            _row(1, "a.py", "auto_approve"),
            _row(2, "a.py", "approve", pushback_type="failure_report"),
        ]
        events = detect_regret_events(rows)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, "failure_report")

    def test_verification_failure_is_regret(self) -> None:
        # An auto-approved write whose verification run failed is an
        # immediate regret — no prior auto-approve needed.
        rows = [
            _row(1, "a.py", "auto_approve", verification_passed=0),
        ]
        events = detect_regret_events(rows)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, "verification_failed")

    def test_different_file_does_not_pollute(self) -> None:
        rows = [
            _row(1, "a.py", "auto_approve"),
            _row(2, "b.py", "deny"),
        ]
        self.assertEqual(detect_regret_events(rows), [])

    def test_each_auto_approve_counted_once(self) -> None:
        rows = [
            _row(1, "a.py", "auto_approve"),
            _row(2, "a.py", "deny"),
            _row(3, "a.py", "deny"),  # no prior auto-approve → no second regret
        ]
        events = detect_regret_events(rows)
        self.assertEqual(len(events), 1)

    def test_summary_aggregates_events(self) -> None:
        rows = [
            _row(1, "a.py", "auto_approve"),
            _row(2, "a.py", "deny"),
            _row(3, "b.py", "auto_approve"),
            _row(4, "b.py", "approve", pushback_type="failure_report"),
        ]
        s = regret_summary(rows)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["by_reason"]["deny"], 1)
        self.assertEqual(s["by_reason"]["failure_report"], 1)
        self.assertEqual(set(s["files"]), {"a.py", "b.py"})


if __name__ == "__main__":
    unittest.main()
