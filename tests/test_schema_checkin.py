from __future__ import annotations

import unittest

from sc.schema import CheckInMessage, IntentDeclaration, ReadRequest


class CheckInSchemaTests(unittest.TestCase):
    def test_checkin_accepts_assumptions_and_confidence(self) -> None:
        message = CheckInMessage(
            type="check_in",
            reason="API migration decision needed.",
            check_in_type="decision_point",
            content="Two paths with tradeoffs.",
            recommendation="Use option B to keep compatibility.",
            options=["A", "B"],
            assumptions=["Clients can migrate in one sprint."],
            confidence=0.66,
        )
        self.assertEqual(message.assumptions, ["Clients can migrate in one sprint."])
        self.assertAlmostEqual(message.confidence or 0.0, 0.66)
        self.assertEqual(message.recommendation, "Use option B to keep compatibility.")

    def test_checkin_confidence_range_is_enforced(self) -> None:
        with self.assertRaises(Exception):
            CheckInMessage(
                type="check_in",
                reason="Invalid confidence",
                check_in_type="uncertainty",
                content="Need guidance.",
                confidence=1.5,
            )


class RepoPathSchemaTests(unittest.TestCase):
    def test_read_request_rejects_nested_parent_traversal(self) -> None:
        with self.assertRaises(Exception):
            ReadRequest(type="read_request", files=["safe/../../outside.txt"])

    def test_intent_declaration_rejects_nested_parent_traversal(self) -> None:
        with self.assertRaises(Exception):
            IntentDeclaration(
                task_summary="try to escape",
                planned_files=["safe/../../outside.txt"],
                planned_actions=["edit_code"],
                planned_commands=[],
            )

    def test_repo_paths_are_normalized_to_posix_relative_paths(self) -> None:
        request = ReadRequest(type="read_request", files=["src\\./module.py"])
        self.assertEqual(request.files, ["src/module.py"])


if __name__ == "__main__":
    unittest.main()
