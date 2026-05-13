from __future__ import annotations

import unittest

from sc.preferences import PushbackType
from sc.run.checkin_styling import (
    dominant_pushback_type,
    render_adapted_checkin_context,
)


class DominantPushbackTests(unittest.TestCase):
    def test_none_for_empty_history(self) -> None:
        self.assertIsNone(dominant_pushback_type([]))

    def test_none_for_too_few_entries(self) -> None:
        self.assertIsNone(dominant_pushback_type([PushbackType.FAILURE_REPORT.value] * 2))

    def test_failure_dominant(self) -> None:
        seq = [PushbackType.FAILURE_REPORT.value] * 6 + [PushbackType.CORRECTION.value] * 2
        self.assertEqual(dominant_pushback_type(seq), PushbackType.FAILURE_REPORT)

    def test_scope_dominant(self) -> None:
        seq = [PushbackType.SCOPE_CONSTRAINT.value] * 5 + [PushbackType.NON_PUSHBACK.value] * 3
        self.assertEqual(dominant_pushback_type(seq), PushbackType.SCOPE_CONSTRAINT)

    def test_no_dominance_when_split(self) -> None:
        seq = [
            PushbackType.FAILURE_REPORT.value,
            PushbackType.CORRECTION.value,
            PushbackType.SCOPE_CONSTRAINT.value,
            PushbackType.POSITIVE_REDIRECT.value,
            PushbackType.NON_PUSHBACK.value,
        ]
        self.assertIsNone(dominant_pushback_type(seq))


class RenderAdaptedContextSmokeTests(unittest.TestCase):
    def test_no_op_when_no_dominance(self) -> None:
        # Should not raise.
        render_adapted_checkin_context(
            dominant_type=None,
            files=["a.py"],
            blast_radius=2,
            recent_verification_failures=[],
        )

    def test_failure_routing_renders(self) -> None:
        render_adapted_checkin_context(
            dominant_type=PushbackType.FAILURE_REPORT,
            files=["a.py", "b.py"],
            blast_radius=5,
            recent_verification_failures=["tests/test_a.py"],
        )

    def test_scope_routing_renders(self) -> None:
        render_adapted_checkin_context(
            dominant_type=PushbackType.SCOPE_CONSTRAINT,
            files=[f"f{i}.py" for i in range(15)],
            blast_radius=10,
            recent_verification_failures=None,
        )

    def test_unknown_pushback_is_no_op(self) -> None:
        render_adapted_checkin_context(
            dominant_type=PushbackType.CORRECTION,
            files=["a.py"],
            blast_radius=1,
            recent_verification_failures=None,
        )


if __name__ == "__main__":
    unittest.main()
