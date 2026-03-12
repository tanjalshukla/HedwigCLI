from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sc.agent_client import ClaudeClient
from sc.commands.admin import _compile_rule_with_model, _repo_inventory, add_rule
from sc.config import SAConfig
from sc.constraints import ParseResult
from sc.schema import RuleCompilation
from sc.trust_db import HardConstraint, TrustDB


class RuleCompilationTests(unittest.TestCase):
    def test_client_compile_rule_accepts_constraints_and_guidelines(self) -> None:
        payload = {
            "constraints": [
                {
                    "path_pattern": "config/prod/",
                    "read_policy": "always_allow",
                    "write_policy": "always_deny",
                    "reason": "Protect production configs",
                }
            ],
            "behavioral_guidelines": [
                "Only check in for API or schema changes."
            ],
            "unresolved": [],
        }
        client = object.__new__(ClaudeClient)
        client._call = lambda session, max_tokens, temperature: json.dumps(payload)  # type: ignore[attr-defined]

        compiled = client.compile_rule(
            "Never modify production configs under config/prod/. Only check in for API changes."
        )

        self.assertIsInstance(compiled, RuleCompilation)
        self.assertEqual(compiled.constraints[0].path_pattern, "config/prod/*")
        self.assertEqual(compiled.behavioral_guidelines, ["Only check in for API or schema changes."])

    def test_repo_inventory_ignores_internal_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".git").mkdir()
            (root / ".git" / "ignored.txt").write_text("x")
            (root / ".sc").mkdir()
            (root / ".sc" / "ignored.json").write_text("{}")
            (root / "src").mkdir()
            (root / "src" / "api.py").write_text("print('ok')\n")

            inventory = _repo_inventory(root)

            self.assertIn("src/api.py", inventory)
            self.assertNotIn(".git/ignored.txt", inventory)
            self.assertNotIn(".sc/ignored.json", inventory)

    def test_compile_rule_with_model_converts_to_parse_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "config").mkdir()
            (repo_root / "config" / "prod.yaml").write_text("x: 1\n")

            mock_client = MagicMock()
            mock_client.compile_rule.return_value = RuleCompilation.model_validate(
                {
                    "constraints": [
                        {
                            "path_pattern": "config/*",
                            "read_policy": "always_allow",
                            "write_policy": "always_check_in",
                        }
                    ],
                    "behavioral_guidelines": ["Always explain risky config changes."],
                    "unresolved": ["production config"],
                }
            )

            with patch("sc.commands.admin.ClaudeClient", return_value=mock_client):
                parsed = _compile_rule_with_model(
                    repo_root=repo_root,
                    rule="Be careful with production config changes and explain them.",
                    source="manual_rule",
                    model_id="test-model",
                    region="us-east-1",
                )

            self.assertEqual(len(parsed.constraints), 1)
            self.assertEqual(parsed.constraints[0].path_pattern, "config/*")
            self.assertEqual(parsed.constraints[0].write_policy, "always_check_in")
            self.assertEqual(parsed.behavioral_guidelines, ["Always explain risky config changes."])
            self.assertEqual(parsed.unresolved_lines, ["production config"])

    def test_add_rule_persists_constraints_and_guidelines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            db = TrustDB(repo_root / ".sc" / "trust.db")
            parsed = ParseResult(
                constraints=[
                    HardConstraint(
                        path_pattern="task_api/api.py",
                        source="manual_rule",
                        overridable=False,
                        read_policy="always_check_in",
                        write_policy="always_check_in",
                    )
                ],
                behavioral_guidelines=["Only check in for API changes."],
                unresolved_lines=[],
            )

            with patch("sc.commands.admin.require_repo_root", return_value=repo_root), \
                patch("sc.commands.admin.open_trust_db", return_value=db), \
                patch(
                    "sc.commands.admin._resolve_config_or_exit",
                    return_value=SAConfig(model_id="model", aws_region="us-east-1"),
                ), \
                patch("sc.commands.admin._compile_rule_with_model", return_value=parsed):
                add_rule(
                    "Only check in for API changes in task_api/api.py.",
                    source="manual_rule",
                    model_id=None,
                    region=None,
                    yes=True,
                )

            constraints = db.list_constraints(str(repo_root))
            guidelines = db.list_behavioral_guidelines(str(repo_root))
            self.assertEqual(len(constraints), 1)
            self.assertEqual(constraints[0].path_pattern, "task_api/api.py")
            self.assertEqual(len(guidelines), 1)
            self.assertEqual(guidelines[0].guideline, "Only check in for API changes.")


if __name__ == "__main__":
    unittest.main()
