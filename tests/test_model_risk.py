from __future__ import annotations

"""Tests for the adversarial-reviewer risk pass.

The reviewer is invoked via a thin Bedrock wrapper. These tests mock the
client surface — they never make real network calls. The contract under
test:

    - happy path returns the reviewer's (score, rationale) pair
    - any failure (Bedrock error, JSON parse failure, score out of range,
      empty rationale, schema validation failure, timeout) returns the
      (0.5, "") "no opinion" default
    - the cache returns previously-computed results for the same
      (file_path, content) pair without re-invoking the client
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from sc import model_risk
from sc.model_risk import assess_risk_via_model


def _fake_client(text_response: str | None = None, raise_exc: Exception | None = None) -> SimpleNamespace:
    """Build a minimal duck-typed ClaudeClient.

    Exposes ``client.messages.create`` and ``model_id`` — the two attributes
    ``assess_risk_via_model`` reaches for.
    """
    inner = MagicMock()
    if raise_exc is not None:
        inner.messages.create.side_effect = raise_exc
    else:
        block = SimpleNamespace(text=text_response or "")
        inner.messages.create.return_value = SimpleNamespace(content=[block])
    return SimpleNamespace(client=inner, model_id="test-model")


class AssessRiskViaModelTests(unittest.TestCase):
    def setUp(self) -> None:
        model_risk._reset_cache_for_tests()

    def test_happy_path_returns_score_and_rationale(self) -> None:
        client = _fake_client('{"score": 0.72, "rationale": "modifies query logic"}')
        score, rationale = assess_risk_via_model(
            file_path="src/db.py",
            diff_or_content="def q(): ...",
            file_context="# context",
            agent_client=client,
        )
        self.assertAlmostEqual(score, 0.72)
        self.assertEqual(rationale, "modifies query logic")

    def test_malformed_json_returns_no_opinion(self) -> None:
        client = _fake_client("not json at all")
        result = assess_risk_via_model("f.py", "x", "y", client)
        self.assertEqual(result, (0.5, ""))

    def test_score_out_of_range_returns_no_opinion(self) -> None:
        client = _fake_client('{"score": 1.5, "rationale": "way too high"}')
        result = assess_risk_via_model("f.py", "x", "y", client)
        self.assertEqual(result, (0.5, ""))

    def test_negative_score_returns_no_opinion(self) -> None:
        client = _fake_client('{"score": -0.1, "rationale": "neg"}')
        result = assess_risk_via_model("f.py", "x", "y", client)
        self.assertEqual(result, (0.5, ""))

    def test_bedrock_exception_returns_no_opinion(self) -> None:
        client = _fake_client(raise_exc=RuntimeError("Bedrock unavailable"))
        result = assess_risk_via_model("f.py", "x", "y", client)
        self.assertEqual(result, (0.5, ""))

    def test_timeout_returns_no_opinion(self) -> None:
        class _TimeoutErr(Exception):
            pass

        client = _fake_client(raise_exc=_TimeoutErr("APITimeoutError"))
        result = assess_risk_via_model("f.py", "x", "y", client)
        self.assertEqual(result, (0.5, ""))

    def test_schema_validation_missing_rationale(self) -> None:
        client = _fake_client('{"score": 0.4}')
        result = assess_risk_via_model("f.py", "x", "y", client)
        self.assertEqual(result, (0.5, ""))

    def test_schema_validation_empty_rationale(self) -> None:
        # Empty rationale is treated as a soft schema failure — fall back.
        client = _fake_client('{"score": 0.4, "rationale": "   "}')
        result = assess_risk_via_model("f.py", "x", "y", client)
        self.assertEqual(result, (0.5, ""))

    def test_schema_validation_bool_score_rejected(self) -> None:
        # bool is an int subclass; we should reject it as a misuse.
        client = _fake_client('{"score": true, "rationale": "ok"}')
        result = assess_risk_via_model("f.py", "x", "y", client)
        self.assertEqual(result, (0.5, ""))

    def test_none_client_returns_no_opinion(self) -> None:
        result = assess_risk_via_model("f.py", "x", "y", None)
        self.assertEqual(result, (0.5, ""))

    def test_cache_hits_avoid_reinvocation(self) -> None:
        client = _fake_client('{"score": 0.3, "rationale": "looks fine"}')
        first = assess_risk_via_model("f.py", "AAA", "ctx", client)
        second = assess_risk_via_model("f.py", "AAA", "ctx", client)
        self.assertEqual(first, second)
        # Only one underlying call despite two invocations.
        self.assertEqual(client.client.messages.create.call_count, 1)

    def test_cache_keyed_by_content_hash(self) -> None:
        client = _fake_client('{"score": 0.3, "rationale": "fine"}')
        assess_risk_via_model("f.py", "AAA", "ctx", client)
        assess_risk_via_model("f.py", "BBB", "ctx", client)
        self.assertEqual(client.client.messages.create.call_count, 2)

    def test_reviewer_does_not_see_intent(self) -> None:
        # Capture the user prompt and verify it contains only path/diff/context,
        # not anything resembling agent intent fields like "task_summary" or
        # "planned_files".
        client = _fake_client('{"score": 0.5, "rationale": "neutral"}')
        # 0.5 is technically a no-op delta in the heuristic but still a valid
        # parse here; we only care about the prompt contents.
        assess_risk_via_model(
            file_path="src/auth.py",
            diff_or_content="def login(): pass",
            file_context="# previous content",
            agent_client=client,
        )
        kwargs = client.client.messages.create.call_args.kwargs
        # Different system prompt from the agent.
        self.assertIn("reviewer", kwargs["system"].lower())
        # User prompt must NOT carry agent-side framing.
        user_text = kwargs["messages"][0]["content"]
        self.assertNotIn("task_summary", user_text)
        self.assertNotIn("planned_files", user_text)
        self.assertNotIn("intent_declaration", user_text)


if __name__ == "__main__":
    unittest.main()
