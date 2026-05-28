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
        assert classifier.sample_count < MIN_SAMPLES_FOR_LEARNED

        profile = autonomy_profile(SAConfig(model_id="test-model"))
        prompt_required = False
        for path in (
            "demo_recipe_api/recipe_api/models.py",
            "demo_recipe_api/recipe_api/store.py",
        ):
            old = (repo / path).read_text()
            risk = assess_risk(
                repo_root=repo,
                file_path=path,
                old_content=old,
                new_content=old + "\n# demo change\n",
                is_new_file=False,
                diff_size=1,
            )
            history = db.policy_history(str(repo), path, stage="apply")
            policy_input = PolicyInput.from_signals(
                history,
                risk,
                recent_denials=0,
                files_in_action=2,
            )
            scorer, label = select_scorer(db.load_policy_model(str(repo)))
            decision = scorer.decide(
                policy_input,
                proceed_threshold=profile.proceed_threshold,
                flag_threshold=profile.flag_threshold,
            )
            assert label == "heuristic"
            prompt_required = prompt_required or decision.action == "check_in"

        assert prompt_required
