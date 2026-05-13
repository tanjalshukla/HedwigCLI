from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.export_report import generate_html_report, write_report
from sc.ml_policy import build_cold_classifier
from sc.trust_db import TrustDB


class ExportReportSmokeTests(unittest.TestCase):
    def _empty_db(self) -> TrustDB:
        return TrustDB(Path(tempfile.mkdtemp()) / "trust.db")

    def test_empty_report_still_renders(self) -> None:
        db = self._empty_db()
        html = generate_html_report(db, "/tmp/fake_repo")
        # Doctype, title, and footer all present — sanity shape checks.
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Hedwig report", html)
        self.assertIn("Sessions", html)
        # Empty state messages should appear.
        self.assertIn("No sessions", html)
        self.assertIn("No learned model", html)

    def test_report_with_classifier_shows_coefficient_table(self) -> None:
        db = self._empty_db()
        repo = "/tmp/fake_repo"
        db.save_policy_model(repo, build_cold_classifier())
        html = generate_html_report(db, repo)
        # The coefficient table should render.
        self.assertIn("<table>", html)
        # Feature names from the scorer should appear as table rows.
        self.assertIn("prior_approvals", html)

    def test_write_report_produces_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "report.html"
            db = self._empty_db()
            written = write_report(db, "/tmp/fake_repo", out)
            self.assertTrue(written.exists())
            self.assertTrue(out.read_text().startswith("<!DOCTYPE html>"))


if __name__ == "__main__":
    unittest.main()
