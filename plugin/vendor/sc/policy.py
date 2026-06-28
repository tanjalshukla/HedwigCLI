from __future__ import annotations

"""PolicyScorer seam — the function that turns RiskSignals into a decision.

Two adapters satisfy this seam:
  HeuristicScorer  — hand-weighted linear scorer, cold-start behavior.
  PolicyClassifier — online logistic regression (ml_policy.py), activates
                     after MIN_SAMPLES_FOR_LEARNED real decisions.

`select_scorer()` picks the active adapter. `decide_action()` runs the
heuristic path with rich reason strings. `_policy_decision_for_file()`
in run/helpers.py is the convergence point that calls whichever adapter
is active and returns a PolicyDecision.

Weight table for the heuristic scorer is documented in SPEC.md §10.
Do not change weights without updating that table.
"""

# Policy scoring. Two adapters satisfy the PolicyScorer seam:
# - HeuristicScorer: hand-weighted linear scorer; carries cold-start behavior.
# - PolicyClassifier (sc/ml_policy.py): online logistic regression; takes over
#   once enough real developer decisions have been observed.

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Literal, Protocol

if TYPE_CHECKING:
    from .features import RiskSignals
    from .trust_db import PolicyHistory


PolicyAction = Literal["check_in", "proceed", "proceed_flag"]


@dataclass(frozen=True)
class PolicyInput:
    # effective approvals (rubber-stamps are 0.5x weighted via PolicyHistory)
    prior_approvals: float
    prior_denials: int
    avg_response_ms: float | None
    avg_edit_distance: float
    diff_size: int
    blast_radius: int
    is_new_file: bool
    is_security_sensitive: bool
    change_pattern: str | None
    recent_denials: int
    files_in_action: int
    verification_failure_rate: float | None = None
    model_confidence_avg: float | None = None
    model_confidence_samples: int = 0
    # Advisory model-reviewer score. 0.5 = "no opinion" (also the failure
    # default from assess_risk_via_model). The deterministic signals above
    # stay load-bearing; this is an additive feature both scorers consume.
    model_risk_score: float = 0.5

    @classmethod
    def from_signals(
        cls,
        history: "PolicyHistory",
        risk: "RiskSignals",
        *,
        recent_denials: int,
        files_in_action: int,
        verification_failure_rate: float | None = None,
        model_confidence_avg: float | None = None,
        model_confidence_samples: int = 0,
    ) -> "PolicyInput":
        """Build a PolicyInput from a PolicyHistory + RiskSignals pair.

        Single source of truth for assembling scorer inputs from the standard
        history/risk objects. Callers reconstructing a synthetic PolicyInput
        (e.g. regret correction from a stored trace row) should construct
        directly rather than through this factory.
        """
        return cls(
            prior_approvals=history.effective_approvals,
            prior_denials=history.denials,
            avg_response_ms=history.avg_response_ms,
            avg_edit_distance=history.avg_edit_distance or 0.0,
            diff_size=risk.diff_size,
            blast_radius=risk.blast_radius,
            is_new_file=risk.is_new_file,
            is_security_sensitive=risk.is_security_sensitive,
            change_pattern=risk.change_pattern,
            recent_denials=recent_denials,
            files_in_action=files_in_action,
            verification_failure_rate=verification_failure_rate,
            model_confidence_avg=model_confidence_avg,
            model_confidence_samples=model_confidence_samples,
            model_risk_score=risk.model_risk_score,
        )


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    score: float
    reasons: tuple[str, ...]


class PolicyScorer(Protocol):
    """Seam for action-level autonomy scoring. Two adapters today: the
    hand-weighted HeuristicScorer below, and the online PolicyClassifier in
    sc.ml_policy. Callers select via select_scorer()."""

    def score(self, pi: "PolicyInput") -> float: ...
    def ready(self) -> bool: ...
    def decide(
        self,
        pi: "PolicyInput",
        proceed_threshold: float,
        flag_threshold: float,
    ) -> "PolicyDecision": ...


def _bucket(score: float, proceed_threshold: float, flag_threshold: float) -> PolicyAction:
    if score >= proceed_threshold:
        return "proceed"
    if score >= flag_threshold:
        return "proceed_flag"
    return "check_in"


def select_scorer(
    classifier: "PolicyScorer | None",
) -> tuple["PolicyScorer", str]:
    """Pick the scorer to use for this decision and tag which one fired.

    Returns (scorer, label). The label ends up in PolicyDecision.reasons so
    traces record which adapter made the call.
    """
    if classifier is not None and classifier.ready():
        return classifier, "learned"
    return _heuristic_scorer, "heuristic"


def decide_action(
    policy_input: PolicyInput,
    proceed_threshold: float,
    flag_threshold: float,
) -> PolicyDecision:
    score = 0.0
    reasons: list[str] = []

    # --- history signals (strongest influence) ---
    if policy_input.prior_approvals:
        score += policy_input.prior_approvals * 0.4
        reasons.append(f"+history:{policy_input.prior_approvals:.1f} weighted approvals")
    if policy_input.prior_denials:
        score -= policy_input.prior_denials * 0.7
        reasons.append(f"-history:{policy_input.prior_denials} denials")

    # review pace: fast approvals noted but not penalized here —
    # the rubber-stamp discount happens upstream in PolicyHistory.effective_approvals
    if policy_input.avg_response_ms is not None:
        if policy_input.avg_response_ms < 5000:
            reasons.append("~history:quick approvals are down-weighted")
        elif policy_input.avg_response_ms > 15000:
            score += 0.15
            reasons.append("+history:deliberate review pace")

    # edit distance: high corrections → developer heavily modifies agent output
    if policy_input.avg_edit_distance > 0:
        score -= min(policy_input.avg_edit_distance, 1.0) * 0.5
        reasons.append(f"-quality:edit distance {policy_input.avg_edit_distance:.2f}")

    # --- risk signals ---
    # Initial risk weights are heuristic priors; the lab study is intended to
    # provide data for replacing or recalibrating them later.
    if policy_input.diff_size > 80:
        score -= 0.8
        reasons.append("-risk:large diff")
    elif policy_input.diff_size > 30:
        score -= 0.4
        reasons.append("-risk:medium diff")

    if policy_input.blast_radius > 3:
        score -= 0.8
        reasons.append("-risk:multi-file blast radius")

    if policy_input.files_in_action > 4:
        score -= 0.9
        reasons.append("-risk:large multi-file action")
    elif policy_input.files_in_action > 1:
        score -= 0.35
        reasons.append("-risk:multi-file action")

    if policy_input.is_new_file:
        score -= 0.6
        reasons.append("-risk:new file")

    if policy_input.is_security_sensitive:
        score -= 2.0
        reasons.append("-risk:security sensitive")

    # full pattern scoring for semantic risk calibration.
    if policy_input.change_pattern in {"api_change", "data_model_change"}:
        score -= 0.8
        reasons.append("-risk:interface change")
    elif policy_input.change_pattern in {"test_generation", "documentation"}:
        score += 0.3
        reasons.append("+risk:low impact change")
    elif policy_input.change_pattern == "config_change":
        score -= 0.4
        reasons.append("-risk:config change")
    elif policy_input.change_pattern == "dependency_update":
        score -= 0.5
        reasons.append("-risk:dependency update")
    elif policy_input.change_pattern == "error_handling":
        score += 0.1
        reasons.append("+risk:error handling is usually localized")

    # --- session momentum ---
    if policy_input.recent_denials:
        score -= min(policy_input.recent_denials, 3) * 0.7
        reasons.append("-session:recent denials")

    # --- trace-derived quality signals ---
    if policy_input.verification_failure_rate is not None and policy_input.verification_failure_rate > 0.30:
        score -= 0.6
        reasons.append(
            f"-quality:verification failure rate {policy_input.verification_failure_rate:.0%}"
        )

    if (
        policy_input.model_confidence_avg is not None
        and policy_input.model_confidence_samples >= 3
        and policy_input.model_confidence_avg < 0.40
    ):
        score -= 0.3
        reasons.append(
            f"-quality:low model confidence {policy_input.model_confidence_avg:.2f} "
            f"({policy_input.model_confidence_samples} samples)"
        )

    # --- adversarial reviewer signal (advisory) ---
    # model_risk_score: 0.0 = looks safe, 1.0 = high risk, 0.5 = no opinion.
    # Map to [-1, +1] (safe is positive, risky is negative) and weight 0.3 —
    # small enough that the deterministic risk signals dominate, large enough
    # to nudge a borderline decision. Skip when at the no-opinion default
    # (which is also the documented failure fallback from
    # assess_risk_via_model) so the heuristic doesn't pretend to have a
    # signal it doesn't have.
    if abs(policy_input.model_risk_score - 0.5) > 1e-9:
        delta = (0.5 - policy_input.model_risk_score) * 2.0  # +1 safe / -1 risky
        score += 0.3 * delta
        if delta < 0:
            reasons.append(
                f"-risk:adversarial reviewer flagged {policy_input.model_risk_score:.2f}"
            )
        else:
            reasons.append(
                f"+risk:adversarial reviewer cleared {policy_input.model_risk_score:.2f}"
            )

    # --- threshold comparison ---
    if score >= proceed_threshold:
        return PolicyDecision("proceed", score, tuple(reasons))
    if score >= flag_threshold:
        return PolicyDecision("proceed_flag", score, tuple(reasons))
    return PolicyDecision("check_in", score, tuple(reasons))


class HeuristicScorer:
    """Hand-weighted adapter at the PolicyScorer seam. Always ready — it's
    what the system uses before the learned scorer has enough real data."""

    def score(self, pi: "PolicyInput") -> float:
        return decide_action(pi, proceed_threshold=0.0, flag_threshold=0.0).score

    def ready(self) -> bool:
        return True

    def decide(
        self,
        pi: "PolicyInput",
        proceed_threshold: float,
        flag_threshold: float,
    ) -> PolicyDecision:
        return decide_action(pi, proceed_threshold=proceed_threshold, flag_threshold=flag_threshold)


_heuristic_scorer = HeuristicScorer()


def within_scope_budget(files: Iterable[str], scope_budget_files: int) -> bool:
    return len(list(files)) <= scope_budget_files
