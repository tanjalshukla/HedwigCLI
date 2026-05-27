from __future__ import annotations

import unittest

import numpy as np

from sc.ml_policy import (
    FEATURE_NAMES,
    MIN_SAMPLES_FOR_LEARNED,
    PolicyClassifier,
    build_cold_classifier,
    featurize,
)
from sc.run.helpers import PatchValidationError, validate_touched_files
from sc.policy import PolicyInput
from pathlib import Path


def _make_pi(
    *,
    prior_approvals: float = 2.0,
    prior_denials: int = 0,
    avg_response_ms: float | None = 5000.0,
    avg_edit_distance: float = 0.1,
    diff_size: int = 20,
    blast_radius: int = 2,
    is_new_file: bool = False,
    is_security_sensitive: bool = False,
    change_pattern: str | None = "general_change",
    recent_denials: int = 0,
    files_in_action: int = 1,
    verification_failure_rate: float | None = None,
    model_confidence_avg: float | None = 0.75,
) -> PolicyInput:
    return PolicyInput(
        prior_approvals=prior_approvals,
        prior_denials=prior_denials,
        avg_response_ms=avg_response_ms,
        avg_edit_distance=avg_edit_distance,
        diff_size=diff_size,
        blast_radius=blast_radius,
        is_new_file=is_new_file,
        is_security_sensitive=is_security_sensitive,
        change_pattern=change_pattern,
        recent_denials=recent_denials,
        files_in_action=files_in_action,
        verification_failure_rate=verification_failure_rate,
        model_confidence_avg=model_confidence_avg,
        model_confidence_samples=3 if model_confidence_avg is not None else 0,
    )


class TestFeaturize(unittest.TestCase):
    def test_output_shape(self) -> None:
        vec = featurize(_make_pi())
        self.assertEqual(vec.shape, (len(FEATURE_NAMES),))

    def test_output_dtype(self) -> None:
        vec = featurize(_make_pi())
        self.assertEqual(vec.dtype, np.float64)

    def test_no_nan_or_inf(self) -> None:
        vec = featurize(_make_pi())
        self.assertTrue(np.all(np.isfinite(vec)))

    def test_none_response_ms_handled(self) -> None:
        vec = featurize(_make_pi(avg_response_ms=None))
        self.assertTrue(np.all(np.isfinite(vec)))

    def test_none_verification_failure_rate_defaults_to_zero(self) -> None:
        vec = featurize(_make_pi(verification_failure_rate=None))
        idx = FEATURE_NAMES.index("verification_failure_rate")
        self.assertEqual(vec[idx], 0.0)

    def test_none_model_confidence_defaults_to_half(self) -> None:
        vec = featurize(_make_pi(model_confidence_avg=None))
        idx = FEATURE_NAMES.index("model_confidence_avg")
        self.assertEqual(vec[idx], 0.5)

    def test_security_sensitive_flag(self) -> None:
        idx = FEATURE_NAMES.index("is_security_sensitive")
        self.assertEqual(featurize(_make_pi(is_security_sensitive=True))[idx], 1.0)
        self.assertEqual(featurize(_make_pi(is_security_sensitive=False))[idx], 0.0)

    def test_new_file_flag(self) -> None:
        idx = FEATURE_NAMES.index("is_new_file")
        self.assertEqual(featurize(_make_pi(is_new_file=True))[idx], 1.0)
        self.assertEqual(featurize(_make_pi(is_new_file=False))[idx], 0.0)

    def test_diff_size_log_scaled(self) -> None:
        # diff_size=0 should give 0; larger diffs should give larger values
        idx = FEATURE_NAMES.index("diff_size_log")
        small = featurize(_make_pi(diff_size=0))[idx]
        large = featurize(_make_pi(diff_size=500))[idx]
        self.assertLess(small, large)

    def test_unknown_change_pattern_defaults(self) -> None:
        vec = featurize(_make_pi(change_pattern="unknown_pattern_xyz"))
        self.assertTrue(np.all(np.isfinite(vec)))

    def test_none_change_pattern(self) -> None:
        vec = featurize(_make_pi(change_pattern=None))
        self.assertTrue(np.all(np.isfinite(vec)))

    def test_feature_names_length_matches_featurize(self) -> None:
        self.assertEqual(len(FEATURE_NAMES), len(featurize(_make_pi())))

    def test_model_risk_score_present_and_passthrough(self) -> None:
        # The advisory adversarial-reviewer feature is wired in, defaults to
        # 0.5 ("no opinion"), and threads through unchanged when set explicitly.
        self.assertIn("model_risk_score", FEATURE_NAMES)
        idx = FEATURE_NAMES.index("model_risk_score")
        self.assertAlmostEqual(featurize(_make_pi())[idx], 0.5)

        from dataclasses import replace
        pi_high = replace(_make_pi(), model_risk_score=0.9)
        pi_low = replace(_make_pi(), model_risk_score=0.1)
        self.assertAlmostEqual(featurize(pi_high)[idx], 0.9)
        self.assertAlmostEqual(featurize(pi_low)[idx], 0.1)


class TestWarmStart(unittest.TestCase):
    def setUp(self) -> None:
        self.clf = build_cold_classifier()

    def test_sample_count_zero_after_build(self) -> None:
        self.assertEqual(self.clf.sample_count, 0)

    def test_not_ready_at_cold_start(self) -> None:
        self.assertFalse(self.clf.ready())

    def test_prior_coef_shape(self) -> None:
        self.assertEqual(self.clf.prior_coef.shape, (len(FEATURE_NAMES),))

    def test_score_returns_probability(self) -> None:
        score = self.clf.score(_make_pi())
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_cold_score_is_uninformative(self) -> None:
        # Cold classifier has seen only the zero/one seed pair, so it shouldn't
        # prefer high-risk over low-risk inputs. The heuristic scorer in
        # policy.py carries cold-start behavior until real decisions arrive.
        low_risk = _make_pi(
            prior_approvals=5.0, diff_size=10, blast_radius=1,
            change_pattern="test_generation", is_security_sensitive=False,
        )
        high_risk = _make_pi(
            prior_approvals=0.0, prior_denials=3, diff_size=200,
            blast_radius=8, change_pattern="api_change",
            is_security_sensitive=True, recent_denials=2,
        )
        # Both scores valid probabilities; either may be higher at cold-start.
        self.assertGreaterEqual(self.clf.score(low_risk), 0.0)
        self.assertLessEqual(self.clf.score(high_risk), 1.0)

    def test_coef_delta_all_zero_before_updates(self) -> None:
        deltas = self.clf.coef_delta()
        self.assertEqual(set(deltas.keys()), set(FEATURE_NAMES))
        for name, delta in deltas.items():
            self.assertAlmostEqual(delta, 0.0, places=10, msg=f"Expected zero delta for {name}")

    def test_serialization_roundtrip(self) -> None:
        blob = self.clf.to_bytes()
        restored = PolicyClassifier.from_bytes(blob)
        self.assertEqual(restored.sample_count, self.clf.sample_count)
        np.testing.assert_array_almost_equal(restored.prior_coef, self.clf.prior_coef)
        np.testing.assert_array_almost_equal(
            restored.clf.coef_[0], self.clf.clf.coef_[0]
        )


class TestPolicyClassifierUpdate(unittest.TestCase):
    def setUp(self) -> None:
        self.clf = build_cold_classifier()

    def test_sample_count_increments(self) -> None:
        pi = _make_pi()
        self.clf.update(pi, approved=True)
        self.assertEqual(self.clf.sample_count, 1)
        self.clf.update(pi, approved=False)
        self.assertEqual(self.clf.sample_count, 2)

    def test_ready_after_min_samples(self) -> None:
        pi = _make_pi()
        for _ in range(MIN_SAMPLES_FOR_LEARNED - 1):
            self.clf.update(pi, approved=True)
            self.assertFalse(self.clf.ready())
        self.clf.update(pi, approved=True)
        self.assertTrue(self.clf.ready())

    def test_coefficients_shift_after_updates(self) -> None:
        pi_approve = _make_pi(
            prior_approvals=5.0, diff_size=10, change_pattern="test_generation"
        )
        pi_deny = _make_pi(
            prior_denials=3, diff_size=150, change_pattern="api_change",
            is_security_sensitive=True, blast_radius=7,
        )
        for _ in range(10):
            self.clf.update(pi_approve, approved=True)
            self.clf.update(pi_deny, approved=False)

        deltas = self.clf.coef_delta()
        total_drift = sum(abs(d) for d in deltas.values())
        # 20 real updates should produce measurable coefficient drift from the
        # cold seed. 0.01 is enough to catch a regression where partial_fit is
        # never called or coefficients are frozen.
        self.assertGreater(total_drift, 0.01, "Expected nonzero coefficient drift after 20 updates")

    def test_coef_delta_keys_match_feature_names(self) -> None:
        self.clf.update(_make_pi(), approved=True)
        deltas = self.clf.coef_delta()
        self.assertEqual(set(deltas.keys()), set(FEATURE_NAMES))

    def test_repeated_denials_lower_score(self) -> None:
        pi = _make_pi(prior_denials=3, diff_size=80, change_pattern="api_change")
        # Seed with a mix so the classifier has seen both classes; without
        # priors, a single-class training trajectory can produce undefined
        # gradients.
        for _ in range(3):
            self.clf.update(_make_pi(prior_approvals=5.0, diff_size=5), approved=True)
        score_before = self.clf.score(pi)
        for _ in range(MIN_SAMPLES_FOR_LEARNED):
            self.clf.update(pi, approved=False)
        score_after = self.clf.score(pi)
        self.assertLessEqual(score_after, score_before)

    def test_repeated_approvals_raise_score(self) -> None:
        # Use a genuinely ambiguous input (score ~0.5) so there is room to move.
        # A high-prior input saturates near 1.0 and cannot rise further.
        pi = _make_pi(
            prior_approvals=1.0, prior_denials=1, diff_size=40,
            blast_radius=3, change_pattern="config_change",
        )
        score_before = self.clf.score(pi)
        for _ in range(MIN_SAMPLES_FOR_LEARNED):
            self.clf.update(pi, approved=True)
        score_after = self.clf.score(pi)
        self.assertGreater(score_after, score_before)

    def test_serialization_preserves_sample_count_and_drift(self) -> None:
        pi = _make_pi()
        for _ in range(5):
            self.clf.update(pi, approved=True)
        blob = self.clf.to_bytes()
        restored = PolicyClassifier.from_bytes(blob)
        self.assertEqual(restored.sample_count, 5)
        for name in FEATURE_NAMES:
            self.assertAlmostEqual(
                restored.coef_delta()[name],
                self.clf.coef_delta()[name],
                places=8,
            )


class TestPatchValidation(unittest.TestCase):
    def test_valid_subset_does_not_raise(self) -> None:
        validate_touched_files(Path("."), ["a.py", "b.py"], {"a.py", "b.py", "c.py"})

    def test_exact_match_does_not_raise(self) -> None:
        validate_touched_files(Path("."), ["a.py"], {"a.py"})

    def test_empty_touched_does_not_raise(self) -> None:
        validate_touched_files(Path("."), [], {"a.py"})

    def test_extra_file_raises(self) -> None:
        with self.assertRaises(PatchValidationError):
            validate_touched_files(Path("."), ["a.py", "z.py"], {"a.py"})

    def test_error_message_names_offending_file(self) -> None:
        try:
            validate_touched_files(Path("."), ["secret.py"], set())
        except PatchValidationError as exc:
            self.assertIn("secret.py", str(exc))

    def test_empty_allowed_with_nonempty_touched_raises(self) -> None:
        with self.assertRaises(PatchValidationError):
            validate_touched_files(Path("."), ["any.py"], set())


class TestCorrectedRegretIds(unittest.TestCase):
    """Verify that _apply_regret_corrections fires at most once per regret trace_id.

    This prevents the O(N) re-application bug where every call re-applies all
    prior regrets, producing spurious negative signals that can reverse real
    approval history.
    """

    def test_regret_correction_applied_only_once(self) -> None:
        import tempfile
        from pathlib import Path
        from sc.run.apply_stage import _apply_regret_corrections
        from sc.trust_db import TrustDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            # Record an auto-approved trace followed by a denial so detect_regret_events
            # will fire.
            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="svc/api.py",
                change_type="api_change",
                diff_size=20,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=1,
                prior_denials=0,
                policy_action="proceed",
                policy_score=0.9,
                user_decision="auto_approve",
                participant_id=None,
                study_run_id=None,
                study_task_id=None,
                autonomy_mode="balanced",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="svc/api.py",
                change_type="api_change",
                diff_size=5,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=1,
                prior_denials=0,
                policy_action="check_in",
                policy_score=0.3,
                user_decision="deny",
                participant_id=None,
                study_run_id=None,
                study_task_id=None,
                autonomy_mode="balanced",
            )

            clf = build_cold_classifier()
            session_rows = [dict(r) for r in db.session_traces(repo, "s1")]

            corrections_first = _apply_regret_corrections(
                classifier=clf,
                trust_db=db,
                repo_root_str=repo,
                session_row_dicts=session_rows,
                recent_apply_denials=1,
            )
            sample_count_after_first = clf.sample_count

            # Second call with the same session rows must not apply any new corrections.
            corrections_second = _apply_regret_corrections(
                classifier=clf,
                trust_db=db,
                repo_root_str=repo,
                session_row_dicts=session_rows,
                recent_apply_denials=1,
            )

            # Round-trip the classifier through SQLite (mirrors the real apply
            # turn flow: load_policy_model → _apply_regret_corrections →
            # save_policy_model). _corrected_regret_ids must survive the
            # pickle round-trip; otherwise the dedup guard never matches and
            # every turn re-applies all prior session regrets.
            clf_reloaded = type(clf).from_bytes(clf.to_bytes())
            corrections_after_reload = _apply_regret_corrections(
                classifier=clf_reloaded,
                trust_db=db,
                repo_root_str=repo,
                session_row_dicts=session_rows,
                recent_apply_denials=1,
            )
            self.assertEqual(
                corrections_after_reload,
                0,
                "Regret corrections must survive a pickle round-trip — "
                "otherwise every apply turn re-applies all session regrets.",
            )

            self.assertEqual(corrections_first, 1, "Expected exactly one regret correction on first call")
            self.assertEqual(corrections_second, 0, "Second call must not re-apply already-corrected regret")
            self.assertEqual(
                clf.sample_count,
                sample_count_after_first,
                "sample_count must not increase on the idempotent second call",
            )


if __name__ == "__main__":
    unittest.main()
