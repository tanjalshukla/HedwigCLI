from __future__ import annotations

# Online logistic regression policy scorer.
# Warm-started from heuristic priors; updated via partial_fit after each
# developer decision. Persisted per repo_root in SQLite as a pickle blob.

import math
import pickle
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import SGDClassifier
from sklearn.preprocessing import StandardScaler

if TYPE_CHECKING:
    from .policy import PolicyInput


# Ordered feature names — must stay in sync with featurize().
FEATURE_NAMES: list[str] = [
    "prior_approvals",
    "prior_denials",
    "avg_response_ms",
    "avg_edit_distance",
    "diff_size_log",
    "blast_radius",
    "is_new_file",
    "is_security_sensitive",
    "files_in_action",
    "recent_denials",
    "verification_failure_rate",
    "model_confidence_avg",
    "change_pattern_risk",
]

# Risk weight per change_pattern. Fed into featurize() so the learned scorer
# sees change-pattern risk on the same scale as the heuristic. Categories
# themselves are owned by features.CHANGE_PATTERNS — this table only maps
# known categories to scorer-specific weights.
_PATTERN_RISK: dict[str | None, float] = {
    "api_change": -0.8,
    "data_model_change": -0.8,
    "config_change": -0.4,
    "dependency_update": -0.5,
    "error_handling": 0.1,
    "test_generation": 0.3,
    "documentation": 0.3,
    "general_change": 0.0,
    None: 0.0,
}

# Number of real developer decisions required before the learned model
# replaces the heuristic scorer. Below this threshold the heuristic is used.
MIN_SAMPLES_FOR_LEARNED: int = 10


def featurize(pi: "PolicyInput") -> np.ndarray:
    """Map a PolicyInput to a normalized float feature vector."""
    return np.array(
        [
            min(pi.prior_approvals / 10.0, 3.0),
            min(pi.prior_denials / 10.0, 3.0),
            min((pi.avg_response_ms or 0.0) / 30_000.0, 1.0),
            min(pi.avg_edit_distance, 1.0),
            math.log1p(pi.diff_size) / math.log1p(500),
            min(pi.blast_radius / 10.0, 3.0),
            1.0 if pi.is_new_file else 0.0,
            1.0 if pi.is_security_sensitive else 0.0,
            min(pi.files_in_action / 10.0, 3.0),
            min(pi.recent_denials / 3.0, 1.0),
            pi.verification_failure_rate if pi.verification_failure_rate is not None else 0.0,
            pi.model_confidence_avg if pi.model_confidence_avg is not None else 0.5,
            (_PATTERN_RISK.get(pi.change_pattern, 0.0) + 1.0) / 2.0,
        ],
        dtype=np.float64,
    )


# Minimum samples needed before we refit the isotonic calibrator. Below this
# threshold, raw SGD probabilities are used (they'll be near-saturated but
# still directionally correct for threshold comparisons).
_CALIBRATION_MIN_SAMPLES: int = 20


@dataclass
class PolicyClassifier:
    clf: SGDClassifier
    scaler: StandardScaler
    sample_count: int
    prior_coef: np.ndarray  # coefficients at cold-seed time, used for delta display
    # Isotonic regression calibrator. Fitted incrementally on (raw_prob, label)
    # pairs accumulated in _calib_X / _calib_y. Replaces raw predict_proba
    # output once _CALIBRATION_MIN_SAMPLES decisions have accumulated.
    _calibrator: IsotonicRegression | None = None
    _calib_X: list[float] = None  # type: ignore[assignment]
    _calib_y: list[int] = None    # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._calib_X is None:
            self._calib_X = []
        if self._calib_y is None:
            self._calib_y = []

    def update(self, pi: "PolicyInput", approved: bool) -> None:
        """Online update from a single developer decision."""
        x = self.scaler.transform(featurize(pi).reshape(1, -1))
        label = 1 if approved else 0
        self.clf.partial_fit(x, np.array([label]), classes=np.array([0, 1]))
        self.sample_count += 1
        # Accumulate raw probability + true label for calibrator.
        raw_prob = float(self.clf.predict_proba(x)[0, 1])
        self._calib_X.append(raw_prob)
        self._calib_y.append(label)
        # Refit calibrator once we have enough samples.
        if len(self._calib_X) >= _CALIBRATION_MIN_SAMPLES:
            cal = IsotonicRegression(out_of_bounds="clip")
            cal.fit(np.array(self._calib_X), np.array(self._calib_y))
            self._calibrator = cal

    def score(self, pi: "PolicyInput") -> float:
        """Return calibrated approval probability in [0, 1].

        Uses isotonic regression calibration once enough decisions have
        accumulated, giving probabilities that actually live between 0 and 1
        rather than saturating near the extremes.
        """
        x = self.scaler.transform(featurize(pi).reshape(1, -1))
        raw = float(self.clf.predict_proba(x)[0, 1])
        if self._calibrator is not None:
            return float(self._calibrator.predict([raw])[0])
        return raw

    def ready(self) -> bool:
        """True once enough real decisions have been incorporated."""
        return self.sample_count >= MIN_SAMPLES_FOR_LEARNED

    def coef_delta(self) -> dict[str, float]:
        """Signed drift of each coefficient relative to the cold-seed state."""
        current = self.clf.coef_[0]
        return {
            name: float(current[i] - self.prior_coef[i])
            for i, name in enumerate(FEATURE_NAMES)
        }

    def to_bytes(self) -> bytes:
        return pickle.dumps(self)

    @staticmethod
    def from_bytes(data: bytes) -> PolicyClassifier:
        return pickle.loads(data)  # noqa: S301 — first-party SQLite blob only


def build_cold_classifier() -> PolicyClassifier:
    """Build an uninitialized classifier. Contains no learned priors — the
    heuristic scorer in policy.py carries cold-start behavior until real
    developer decisions accumulate (see MIN_SAMPLES_FOR_LEARNED). The scaler
    is fit on a minimal prototype set (zeros + ones in each feature's bounded
    range) so the first update() call doesn't raise NotFittedError.
    """
    n_features = len(FEATURE_NAMES)
    scaler = StandardScaler()
    scaler.fit(np.array([np.zeros(n_features), np.ones(n_features)]))

    clf = SGDClassifier(
        loss="log_loss",
        penalty="l2",
        alpha=0.001,
        max_iter=1,
        warm_start=True,
        random_state=42,
    )
    # Seed the classifier with a single zero+one pair so partial_fit has seen
    # both classes; this state is replaced by real data as it arrives.
    seed_X = scaler.transform(np.array([np.zeros(n_features), np.ones(n_features)]))
    clf.partial_fit(seed_X, np.array([0, 1]), classes=np.array([0, 1]))

    return PolicyClassifier(
        clf=clf,
        scaler=scaler,
        sample_count=0,
        prior_coef=clf.coef_[0].copy(),
        _calibrator=None,
        _calib_X=[],
        _calib_y=[],
    )


