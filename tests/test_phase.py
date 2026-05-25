from __future__ import annotations

import unittest

from sc.plan_gate import evaluate_write_phase_gate, PhaseGateResult


class PhaseGateTests(unittest.TestCase):
    def test_research_blocks_all_writes(self) -> None:
        result = evaluate_write_phase_gate("research", ["a.py", "notes.md"])
        self.assertFalse(result.allowed)
        self.assertEqual(result.blocked_files, ["a.py", "notes.md"])

    def test_planning_allows_markdown_only(self) -> None:
        result = evaluate_write_phase_gate("planning", ["docs/plan.md", "src/app.py"])
        self.assertFalse(result.allowed)
        self.assertEqual(result.blocked_files, ["src/app.py"])

    def test_implementation_allows_normal_writes(self) -> None:
        result = evaluate_write_phase_gate("implementation", ["src/app.py"])
        self.assertTrue(result.allowed)
        self.assertEqual(result.blocked_files, [])


if __name__ == "__main__":
    unittest.main()
