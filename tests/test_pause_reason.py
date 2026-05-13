from __future__ import annotations

import unittest

from sc.policy import PolicyDecision
from sc.run.pause_reason import synthesize_pause_reason


def _decision(*reasons: str) -> PolicyDecision:
    return PolicyDecision(action="check_in", score=0.0, reasons=reasons)


class SynthesizePauseReasonTests(unittest.TestCase):
    def test_returns_none_when_no_known_reason(self) -> None:
        self.assertIsNone(
            synthesize_pause_reason({"a.py": _decision("mystery reason")}, ["a.py"])
        )

    def test_picks_hard_constraint_over_risk(self) -> None:
        out = synthesize_pause_reason(
            {
                "a.py": _decision(
                    "hard constraint: always_check_in",
                    "-risk:new file",
                )
            },
            ["a.py"],
        )
        assert out is not None
        self.assertIn("hard constraint", out)

    def test_picks_confirmed_preference_over_risk(self) -> None:
        out = synthesize_pause_reason(
            {
                "a.py": _decision(
                    "-risk:new file",
                    "confirmed preference forced check-in",
                )
            },
            ["a.py"],
        )
        assert out is not None
        self.assertIn("preference you confirmed", out)

    def test_uses_security_before_large_diff(self) -> None:
        out = synthesize_pause_reason(
            {
                "a.py": _decision(
                    "-risk:large diff",
                    "-risk:security sensitive",
                )
            },
            ["a.py"],
        )
        assert out is not None
        self.assertIn("security-sensitive", out)

    def test_counts_multiple_files(self) -> None:
        out = synthesize_pause_reason(
            {
                "a.py": _decision("-risk:new file"),
                "b.py": _decision("-risk:new file"),
            },
            ["a.py", "b.py"],
        )
        assert out is not None
        self.assertIn("2 of these files", out)


if __name__ == "__main__":
    unittest.main()
