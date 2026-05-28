from __future__ import annotations

import unittest
from unittest.mock import patch

from sc.run.ui import _prompt_read


class RunUiPromptTests(unittest.TestCase):
    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_approve_once(self, ask_mock) -> None:
        ask_mock.return_value = "a"
        approved, denied, remember, note = _prompt_read(
            ["task_api/api.py"], "Need to inspect the handler."
        )
        self.assertEqual(approved, ["task_api/api.py"])
        self.assertEqual(denied, [])
        self.assertEqual(remember, [])
        self.assertIsNone(note)

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_approve_and_remember_single(self, ask_mock) -> None:
        ask_mock.return_value = "r"
        approved, denied, remember, note = _prompt_read(
            ["task_api/api.py"], "Need to inspect the handler."
        )
        self.assertEqual(approved, ["task_api/api.py"])
        self.assertEqual(denied, [])
        self.assertEqual(remember, ["task_api/api.py"])
        self.assertIsNone(note)

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_deny(self, ask_mock) -> None:
        ask_mock.side_effect = ["d", "not this time"]
        approved, denied, remember, note = _prompt_read(
            ["task_api/api.py"], "Need to inspect the handler."
        )
        self.assertEqual(approved, [])
        self.assertEqual(denied, ["task_api/api.py"])
        self.assertEqual(remember, [])
        self.assertEqual(note, "not this time")

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_multi_approve_all(self, ask_mock) -> None:
        ask_mock.return_value = "a"
        approved, denied, remember, note = _prompt_read(
            ["task_api/api.py", "task_api/service.py"], None
        )
        self.assertEqual(approved, ["task_api/api.py", "task_api/service.py"])
        self.assertEqual(denied, [])
        self.assertEqual(remember, [])
        self.assertIsNone(note)

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_multi_remember_all(self, ask_mock) -> None:
        ask_mock.return_value = "r"
        approved, denied, remember, note = _prompt_read(
            ["task_api/api.py", "task_api/service.py"], None
        )
        self.assertEqual(approved, ["task_api/api.py", "task_api/service.py"])
        self.assertEqual(denied, [])
        self.assertEqual(remember, ["task_api/api.py", "task_api/service.py"])
        self.assertIsNone(note)

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_multi_select_partial(self, ask_mock) -> None:
        # Top-level 's', then per-file: a (approve), r (approve+remember),
        # d (deny). Trailing prompt asks for denial reason.
        ask_mock.side_effect = ["s", "a", "r", "d", "skip auth"]
        approved, denied, remember, note = _prompt_read(
            ["task_api/api.py", "task_api/service.py", "task_api/auth.py"],
            None,
        )
        self.assertEqual(approved, ["task_api/api.py", "task_api/service.py"])
        self.assertEqual(denied, ["task_api/auth.py"])
        self.assertEqual(remember, ["task_api/service.py"])
        self.assertEqual(note, "skip auth")

    @patch("sc.run.ui.Prompt.ask")
    def test_prompt_read_multi_select_all_approved(self, ask_mock) -> None:
        # Banging Enter through select picker → defaults to 'a' on each file.
        ask_mock.side_effect = ["s", "a", "a"]
        approved, denied, remember, note = _prompt_read(
            ["task_api/api.py", "task_api/service.py"], None
        )
        self.assertEqual(approved, ["task_api/api.py", "task_api/service.py"])
        self.assertEqual(denied, [])
        self.assertEqual(remember, [])
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
