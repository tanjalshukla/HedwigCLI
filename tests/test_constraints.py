from __future__ import annotations

from pathlib import Path
import unittest

from sc.constraints import compile_manual_constraint_rule, parse_constraints_file, parse_constraints_from_text


class ConstraintParserTests(unittest.TestCase):
    def test_parses_deny_checkin_allow_rules(self) -> None:
        text = """
        Do not modify files in src/auth/
        Always check in for `billing/`
        Always allow docs/*.md
        """
        parsed = parse_constraints_from_text(text, source="AGENTS.md")
        rows = {(item.path_pattern, item.constraint_type) for item in parsed.constraints}
        self.assertIn(("src/auth/*", "always_deny"), rows)
        self.assertIn(("billing/*", "always_check_in"), rows)
        self.assertIn(("docs/*.md", "always_allow"), rows)
        self.assertEqual(parsed.unresolved_lines, [])

    def test_ambiguous_rule_falls_back_to_check_in(self) -> None:
        text = "Be careful with billing logic."
        parsed = parse_constraints_from_text(text, source="AGENTS.md")
        self.assertEqual(len(parsed.constraints), 0)
        self.assertIn("Be careful with billing logic.", parsed.behavioral_guidelines)
        self.assertEqual(len(parsed.unresolved_lines), 1)

    def test_parses_fixture_file(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "sample_agents.md"
        parsed = parse_constraints_file(fixture)
        rows = {(item.path_pattern, item.constraint_type) for item in parsed.constraints}
        self.assertIn(("src/auth/*", "always_deny"), rows)
        self.assertIn(("shared/types.py", "always_check_in"), rows)
        self.assertIn(("docs/*.md", "always_allow"), rows)
        self.assertIn("Be careful with billing module changes", parsed.behavioral_guidelines)

    def test_parses_split_read_write_rule(self) -> None:
        text = "Allow reads for `src/frontend/*` but check in before writing to the same files."
        parsed = parse_constraints_from_text(text, source="AGENTS.md")
        self.assertEqual(len(parsed.constraints), 1)
        constraint = parsed.constraints[0]
        self.assertEqual(constraint.read_policy, "always_allow")
        self.assertEqual(constraint.write_policy, "always_check_in")

    def test_compiles_manual_path_constraint_sentence_to_write_deny(self) -> None:
        text = "Never modify files under `config/prod/*`."
        parsed = compile_manual_constraint_rule(text, source="manual_rule")
        self.assertEqual(len(parsed.constraints), 1)
        constraint = parsed.constraints[0]
        self.assertEqual(constraint.path_pattern, "config/prod/*")
        self.assertEqual(constraint.write_policy, "always_deny")
        self.assertEqual(constraint.read_policy, "always_allow")


if __name__ == "__main__":
    unittest.main()
