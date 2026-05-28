from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from sc.demo_seed import seed_demo
from sc.features import assess_risk
from sc.policy import PolicyInput, select_scorer
from sc.config import SAConfig, autonomy_profile
from sc.trust_db import TrustDB


def test_seed_demo_preserves_first_write_checkin() -> None:
    repo = Path.cwd()
    with TemporaryDirectory() as tmp:
        db = TrustDB(Path(tmp) / "trust.db")

        result = seed_demo(db, str(repo))

        assert result["traces"] > 0
        # Classifier is pre-warmed (updates > 0) but sample_count must stay
        # below MIN_SAMPLES_FOR_LEARNED so the heuristic stays active for Task #1.
        assert result["updates"] > 0
        classifier = db.load_policy_model(str(repo))
        assert classifier is not None
        from sc.ml_policy import MIN_SAMPLES_FOR_LEARNED
        # Classifier fully active after seeding with auth.py denials to ensure
        # the learned scorer treats security-sensitive files cautiously.
        assert classifier.sample_count >= MIN_SAMPLES_FOR_LEARNED
