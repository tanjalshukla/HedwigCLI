from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.features import (
    CHANGE_PATTERNS,
    RiskSignals,
    assess_risk,
    change_type_label,
    classify_change_pattern,
    estimate_blast_radius,
    is_security_sensitive,
)


class FeatureTests(unittest.TestCase):
    def test_change_pattern_classification(self) -> None:
        self.assertEqual(
            classify_change_pattern("tests/test_cli.py", "", "def test_x():\n    assert True\n"),
            "test_generation",
        )
        self.assertEqual(
            classify_change_pattern("settings/config.toml", "", "x = 1\n"),
            "config_change",
        )
        self.assertEqual(
            classify_change_pattern("src/api/routes.py", "", "def handler():\n    pass\n"),
            "api_change",
        )

    def test_security_detection(self) -> None:
        self.assertTrue(is_security_sensitive("src/auth/token.py", "def f():\n    pass\n"))
        self.assertTrue(is_security_sensitive("src/core.py", "uses oauth token"))
        self.assertFalse(is_security_sensitive("src/core.py", "plain helper"))

    def test_blast_radius_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text("def run():\n    return 1\n")
            (root / "pkg" / "b.py").write_text("from pkg.a import run\n")
            (root / "pkg" / "c.py").write_text("import pkg.a\n")
            radius = estimate_blast_radius(root, "pkg/a.py")
            self.assertGreaterEqual(radius, 2)

    def test_blast_radius_skips_dependency_dirs(self) -> None:
        """The walk must prune .venv / node_modules / .git etc. — importer
        matches inside a vendored dependency tree are not real blast radius,
        and scanning them added seconds of latency per edit on real repos."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "pkg").mkdir()
            (root / "pkg" / "a.py").write_text("def run():\n    return 1\n")
            (root / "pkg" / "b.py").write_text("from pkg.a import run\n")
            # A vendored copy that also imports pkg.a — must NOT be counted.
            venv = root / ".venv" / "lib"
            venv.mkdir(parents=True)
            (venv / "shadow.py").write_text("from pkg.a import run\n")
            radius = estimate_blast_radius(root, "pkg/a.py")
            # Only pkg/b.py counts; the .venv match is pruned.
            self.assertEqual(radius, 1)


class AssessRiskTests(unittest.TestCase):
    def test_assess_risk_aggregates_all_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "api").mkdir()
            (root / "api" / "routes.py").write_text("def handler(): pass\n")
            signals = assess_risk(
                repo_root=root,
                file_path="api/routes.py",
                old_content="def handler(): pass\n",
                new_content="def handler(): return 1\n",
                is_new_file=False,
                diff_size=12,
            )
        self.assertIsInstance(signals, RiskSignals)
        self.assertEqual(signals.change_pattern, "api_change")
        self.assertEqual(signals.diff_size, 12)
        self.assertFalse(signals.is_new_file)

    def test_change_type_label_prefixes_new_file(self) -> None:
        new = RiskSignals(
            change_pattern="general_change", blast_radius=1,
            is_security_sensitive=False, is_new_file=True, diff_size=5,
        )
        existing = RiskSignals(
            change_pattern="general_change", blast_radius=1,
            is_security_sensitive=False, is_new_file=False, diff_size=5,
        )
        self.assertEqual(change_type_label(new), "new_file:general_change")
        self.assertEqual(change_type_label(existing), "general_change")

    def test_all_classifier_outputs_in_canonical_vocabulary(self) -> None:
        # Any pattern returned by classify_change_pattern must be known to
        # features.CHANGE_PATTERNS — downstream scorers rely on this.
        for path, old, new in [
            ("tests/test_x.py", "", "def test(): pass"),
            ("settings.yaml", "", "k: 1"),
            ("src/api/routes.py", "", "def h(): pass"),
            ("src/models.py", "", "class M: pass"),
            ("src/util.py", "x", "try:\n    x\nexcept Exception:\n    pass"),
            ("src/util.py", "x", "import foo\nx"),
            ("docs/readme.md", "", "# hi"),
            ("src/util.py", "", "def f(): pass"),
        ]:
            pat = classify_change_pattern(path, old, new)
            self.assertIn(pat, CHANGE_PATTERNS)


if __name__ == "__main__":
    unittest.main()
