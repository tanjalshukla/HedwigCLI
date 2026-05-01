from __future__ import annotations

# Online logistic regression policy scorer.
# Warm-started from heuristic priors; updated via partial_fit after each
# developer decision. Persisted per repo_root in SQLite as a pickle blob.

import math
import pickle
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
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

# Risk weight per change_pattern, mirroring the additive scores in policy.py
# so synthetic warm-start labels match the heuristic exactly.
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


@dataclass
class PolicyClassifier:
    clf: SGDClassifier
    scaler: StandardScaler
    sample_count: int
    prior_coef: np.ndarray  # coefficients at warm-start, used for delta display

    def update(self, pi: "PolicyInput", approved: bool) -> None:
        """Online update from a single developer decision."""
        x = self.scaler.transform(featurize(pi).reshape(1, -1))
        self.clf.partial_fit(x, np.array([1 if approved else 0]), classes=np.array([0, 1]))
        self.sample_count += 1

    def score(self, pi: "PolicyInput") -> float:
        """Return approval probability in [0, 1]."""
        x = self.scaler.transform(featurize(pi).reshape(1, -1))
        return float(self.clf.predict_proba(x)[0, 1])

    def ready(self) -> bool:
        """True once enough real decisions have been incorporated."""
        return self.sample_count >= MIN_SAMPLES_FOR_LEARNED

    def coef_delta(self) -> dict[str, float]:
        """Signed drift of each coefficient relative to warm-start priors."""
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


def _build_warm_start_data(n: int) -> tuple[np.ndarray, np.ndarray]:
    from .policy import PolicyInput, decide_action

    rng = np.random.default_rng(42)

    prior_approvals = rng.uniform(0, 8, n)
    prior_denials = rng.integers(0, 5, n).astype(float)
    avg_response_ms = np.where(rng.random(n) < 0.2, None, rng.uniform(1_000, 40_000, n))
    avg_edit_distance = rng.uniform(0, 1, n)
    diff_sizes = rng.integers(0, 300, n)
    blast_radii = rng.integers(1, 8, n)
    is_new_file = rng.random(n) < 0.25
    is_security_sensitive = rng.random(n) < 0.1
    change_patterns = rng.choice(list(_PATTERN_RISK.keys()), n)  # type: ignore[arg-type]
    recent_denials = rng.integers(0, 4, n)
    files_in_action = rng.integers(1, 6, n)
    verification_failure_rates = np.where(rng.random(n) < 0.3, rng.uniform(0, 1, n), None)
    model_confidence = np.where(rng.random(n) < 0.4, rng.uniform(0.2, 1.0, n), None)

    X: list[np.ndarray] = []
    y: list[int] = []
    for i in range(n):
        pi = PolicyInput(
            prior_approvals=float(prior_approvals[i]),
            prior_denials=int(prior_denials[i]),
            avg_response_ms=float(avg_response_ms[i]) if avg_response_ms[i] is not None else None,
            avg_edit_distance=float(avg_edit_distance[i]),
            diff_size=int(diff_sizes[i]),
            blast_radius=int(blast_radii[i]),
            is_new_file=bool(is_new_file[i]),
            is_security_sensitive=bool(is_security_sensitive[i]),
            change_pattern=change_patterns[i],
            recent_denials=int(recent_denials[i]),
            files_in_action=int(files_in_action[i]),
            verification_failure_rate=float(verification_failure_rates[i]) if verification_failure_rates[i] is not None else None,
            model_confidence_avg=float(model_confidence[i]) if model_confidence[i] is not None else None,
            model_confidence_samples=3 if model_confidence[i] is not None else 0,
        )
        score = float(decide_action(pi, proceed_threshold=0.9, flag_threshold=0.2).score)
        X.append(featurize(pi))
        y.append(1 if score >= 0.2 else 0)

    return np.array(X), np.array(y)


def build_warm_start_classifier(n_synthetic: int = 500) -> PolicyClassifier:
    """Build a classifier pre-trained on synthetic heuristic data so cold-start behavior is stable."""
    X, y = _build_warm_start_data(n_synthetic)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    clf = SGDClassifier(loss="log_loss", penalty="l2", alpha=0.001, max_iter=1, warm_start=True, random_state=42)
    for _ in range(20):
        clf.partial_fit(X_scaled, y, classes=np.array([0, 1]))

    return PolicyClassifier(clf=clf, scaler=scaler, sample_count=0, prior_coef=clf.coef_[0].copy())
