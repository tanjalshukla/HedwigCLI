from __future__ import annotations

import unittest

from sc.checkin_quality import evaluate_checkin_quality
from sc.schema import CheckInMessage


class CheckInQualityTests(unittest.TestCase):
    def test_accepts_architectural_tradeoff_checkin(self) -> None:
        message = CheckInMessage(
            type="check_in",
            check_in_type="decision_point",
            reason="Pagination strategy impacts API contract and cache consistency.",
            content=(
                "Option A keeps offset pagination with minimal schema changes but has drift risk at scale. "
                "Option B moves to cursor pagination, improves consistency, and reduces duplicate rows under writes. "
                "The tradeoff is client migration cost and added token parsing logic. "
                "I recommend Option B because it is safer for future throughput and aligns with the existing streaming workflow design."
            ),
            options=["Keep offset pagination", "Migrate to cursor pagination"],
            assumptions=[
                "Existing clients support cursor tokens.",
                "Write volume will increase over the next quarter.",
            ],
            confidence=0.71,
        )
        result = evaluate_checkin_quality(message)
        self.assertTrue(result.valid)

    def test_rejects_shallow_checkin(self) -> None:
        message = CheckInMessage(
            type="check_in",
            check_in_type="decision_point",
            reason="Need input",
            content="Should we do A or B?",
            options=["A"],
        )
        result = evaluate_checkin_quality(message)
        self.assertFalse(result.valid)
        self.assertGreaterEqual(len(result.issues), 3)

    def test_accepts_api_tradeoff_checkin_without_literal_architecture_word(self) -> None:
        message = CheckInMessage(
            type="check_in",
            check_in_type="decision_point",
            reason="Adding a summary endpoint changes the API surface and response contract.",
            content=(
                "Option A extends the existing list handler and keeps one route, but it risks mixing summary data "
                "into the current response envelope. Option B adds a dedicated summary endpoint, keeps the list "
                "handler stable, and makes the route contract easier to reason about. The tradeoff is one extra "
                "endpoint versus less coupling. I recommend Option B because it preserves the current handler "
                "behavior and isolates the new response shape."
            ),
            options=["Extend the existing list handler", "Add a dedicated summary endpoint"],
            assumptions=["The current list handler should remain backward compatible."],
            confidence=0.74,
        )
        result = evaluate_checkin_quality(message)
        self.assertTrue(result.valid)


if __name__ == "__main__":
    unittest.main()
