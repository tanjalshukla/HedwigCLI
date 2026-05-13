from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sc.ml_policy import build_cold_classifier
from sc.trust_db import TrustDB


class PolicyModelRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "trust.db"
        self.trust_db = TrustDB(self.db_path)
        self.repo_root = "/tmp/fake_repo"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_save_creates_snapshot_of_prior(self) -> None:
        c1 = build_cold_classifier()
        self.trust_db.save_policy_model(self.repo_root, c1)
        # First save has no prior — no snapshot should exist yet.
        self.assertEqual(self.trust_db.list_policy_model_snapshots(self.repo_root), [])

        c2 = build_cold_classifier()
        self.trust_db.save_policy_model(self.repo_root, c2)
        # Second save should have snapshotted the first.
        snaps = self.trust_db.list_policy_model_snapshots(self.repo_root)
        self.assertEqual(len(snaps), 1)

    def test_rollback_restores_and_consumes_snapshot(self) -> None:
        self.trust_db.save_policy_model(self.repo_root, build_cold_classifier())
        self.trust_db.save_policy_model(self.repo_root, build_cold_classifier())
        pre = self.trust_db.list_policy_model_snapshots(self.repo_root)
        self.assertEqual(len(pre), 1)

        ok = self.trust_db.restore_policy_model_snapshot(self.repo_root)
        self.assertTrue(ok)
        post = self.trust_db.list_policy_model_snapshots(self.repo_root)
        self.assertEqual(post, [])

    def test_rollback_without_snapshot_returns_false(self) -> None:
        self.assertFalse(
            self.trust_db.restore_policy_model_snapshot(self.repo_root)
        )

    def test_retention_prunes_old_snapshots(self) -> None:
        for _ in range(25):
            self.trust_db.save_policy_model(self.repo_root, build_cold_classifier())
        snaps = self.trust_db.list_policy_model_snapshots(self.repo_root)
        # Retention is _SNAPSHOT_RETENTION = 20.
        self.assertLessEqual(len(snaps), 20)


if __name__ == "__main__":
    unittest.main()
