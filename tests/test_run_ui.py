from __future__ import annotations

import unittest
from unittest.mock import patch

from sc.run.ui import _prompt_read


class RunUiPromptTests(unittest.TestCase):
    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_approve_once(self, ask_mock) -> None:
        ask_mock.return_value = "a"
        approved, remember_paths, note = _prompt_read(["task_api/api.py"], "Need to inspect the handler.")
        self.assertTrue(approved)
        self.assertEqual(remember_paths, [])
        self.assertIsNone(note)

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_approve_and_remember_single(self, ask_mock) -> None:
        ask_mock.return_value = "r"
        approved, remember_paths, note = _prompt_read(["task_api/api.py"], "Need to inspect the handler.")
        self.assertTrue(approved)
        self.assertEqual(remember_paths, ["task_api/api.py"])
        self.assertIsNone(note)

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_deny(self, ask_mock) -> None:
        ask_mock.side_effect = ["d", "not this time"]
        approved, remember_paths, note = _prompt_read(["task_api/api.py"], "Need to inspect the handler.")
        self.assertFalse(approved)
        self.assertEqual(remember_paths, [])
        self.assertEqual(note, "not this time")

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_multi_approve_all(self, ask_mock) -> None:
        ask_mock.return_value = "a"
        approved, remember_paths, note = _prompt_read(
            ["task_api/api.py", "task_api/service.py"], None
        )
        self.assertTrue(approved)
        self.assertEqual(remember_paths, [])
        self.assertIsNone(note)

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_multi_remember_subset(self, ask_mock) -> None:
        # Top-level prompt → 'p'. Per-file toggles: remember first, skip second.
        ask_mock.side_effect = ["p", "y", "n"]
        approved, remember_paths, note = _prompt_read(
            ["task_api/api.py", "task_api/service.py"], None
        )
        self.assertTrue(approved)
        self.assertEqual(remember_paths, ["task_api/api.py"])
        self.assertIsNone(note)

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_multi_remember_all(self, ask_mock) -> None:
        ask_mock.return_value = "r"
        approved, remember_paths, note = _prompt_read(
            ["task_api/api.py", "task_api/service.py"], None
        )
        self.assertTrue(approved)
        self.assertEqual(remember_paths, ["task_api/api.py", "task_api/service.py"])
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
