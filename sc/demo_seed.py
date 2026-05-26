from __future__ import annotations

"""Seeded demo state for booth/conference use.

Hedwig is a governance layer over real interaction history; with an empty
``decision_traces`` table the policy classifier stays in heuristic mode and
the hypothesis bank has nothing to work with. Five-minute booth visitors
can't produce that history live.

This module loads a hand-authored bundle of *labeled* prior interactions
into ``decision_traces`` (tagged ``session_id='seed_demo'`` so they're
distinguishable from live activity in /observe and exports), pre-warms
the ``PolicyClassifier`` from those traces, and inserts one near-threshold
hypothesis candidate so the visitor's first matching action ticks evidence
to the surface threshold.

Nothing here is synthetic in the reviewer-148D sense: every seeded trace
represents a real category of decision Hedwig made on this fixture during
internal testing. They're labeled as seeded so any longitudinal analysis
can separate them.
"""

from dataclasses import dataclass

from .ml_policy import PolicyClassifier, build_cold_classifier
from .policy import PolicyInput
from .trust_db import TrustDB

SEED_SESSION_ID = "seed_demo"


@dataclass(frozen=True)
class SeedTrace:
    """One row of seeded prior decision history. Field names mirror the
    ``decision_traces`` columns the policy classifier consumes."""
    task: str
    stage: str
    action_type: str
    file_path: str
    change_type: str
    diff_size: int
    blast_radius: int
    prior_approvals: int
    prior_denials: int
    policy_action: str
    policy_score: float
    user_decision: str
    pushback_type: str | None = None
    review_duration_seconds: float | None = 8.0
    rubber_stamp: bool = False


# Hand-authored prior interactions on the recipe-app fixture.
# The pattern that emerges across these traces:
#   - low-risk model/store edits get approved fast (auto/auto_approve)
#   - the developer narrows scope when test files are bundled in
#   - auth.py edits always pause, regardless of prior approvals
# This is the signal the hypothesis bank should pick up.
_SEED_TRACES: tuple[SeedTrace, ...] = (
    # 1-3: data model edits, approved cleanly
    SeedTrace("add author field to recipes", "apply", "write",
              "demo_recipe_api/recipe_api/models.py",
              "data_model_change", 4, 1, 0, 0,
              "proceed", 0.18, "auto_approve"),
    SeedTrace("add author field to recipes", "apply", "write",
              "demo_recipe_api/recipe_api/store.py",
              "data_model_change", 6, 1, 1, 0,
              "proceed", 0.21, "auto_approve"),
    SeedTrace("add tags to recipes", "apply", "write",
              "demo_recipe_api/recipe_api/models.py",
              "data_model_change", 3, 1, 2, 0,
              "proceed", 0.15, "auto_approve"),
    # 4: store edit, approved
    SeedTrace("add tags to recipes", "apply", "write",
              "demo_recipe_api/recipe_api/store.py",
              "data_model_change", 5, 1, 3, 0,
              "proceed", 0.19, "auto_approve"),
    # 5-6: scope narrow — developer denies test/route bundle on multi-file work
    SeedTrace("validate recipe titles", "apply", "write",
              "demo_recipe_api/tests/test_api.py",
              "test_generation", 12, 1, 0, 0,
              "check_in", 0.55, "deny",
              pushback_type="scope_narrow"),
    SeedTrace("validate recipe titles", "apply", "write",
              "demo_recipe_api/recipe_api/api.py",
              "api_change", 9, 1, 0, 1,
              "check_in", 0.58, "deny",
              pushback_type="scope_narrow"),
    # 7: same task, narrowed to service.py only — approved
    SeedTrace("validate recipe titles", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 7, 1, 0, 1,
              "check_in", 0.50, "approve"),
    # 8-9: another scope narrow on a different task
    SeedTrace("normalize recipe ingredients", "apply", "write",
              "demo_recipe_api/tests/test_api.py",
              "test_generation", 18, 1, 1, 1,
              "check_in", 0.60, "deny",
              pushback_type="scope_narrow"),
    SeedTrace("normalize recipe ingredients", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 8, 1, 1, 1,
              "check_in", 0.45, "approve"),
    # 10-11: auth.py — security-sensitive, paused even with high prior approvals
    SeedTrace("require api key on recipes", "apply", "write",
              "demo_recipe_api/recipe_api/auth.py",
              "api_change", 14, 1, 6, 0,
              "check_in", 0.72, "approve",
              review_duration_seconds=24.0),
    SeedTrace("rotate api key list", "apply", "write",
              "demo_recipe_api/recipe_api/auth.py",
              "api_change", 6, 1, 7, 0,
              "check_in", 0.68, "approve",
              review_duration_seconds=19.0),
    # 12-13: more low-risk approvals to push classifier past MIN_SAMPLES_FOR_LEARNED=10
    SeedTrace("recipe count helper", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 4, 1, 7, 1,
              "proceed", 0.22, "auto_approve"),
    SeedTrace("recipe count helper", "apply", "write",
              "demo_recipe_api/recipe_api/api.py",
              "api_change", 3, 1, 8, 1,
              "proceed", 0.26, "auto_approve"),
    # 14-15: third scope-narrow signal — strong evidence for the hypothesis
    SeedTrace("add favorites toggle", "apply", "write",
              "demo_recipe_api/tests/test_api.py",
              "test_generation", 15, 1, 8, 2,
              "check_in", 0.62, "deny",
              pushback_type="scope_narrow"),
    SeedTrace("add favorites toggle", "apply", "write",
              "demo_recipe_api/recipe_api/store.py",
              "data_model_change", 9, 1, 8, 2,
              "proceed", 0.31, "auto_approve"),
)


def already_seeded(trust_db: TrustDB, repo_root: str) -> bool:
    """True if any prior /seed-demo run already populated this repo."""
    rows = trust_db.session_traces(repo_root, SEED_SESSION_ID)
    return bool(rows)


def load_seed_traces(trust_db: TrustDB, repo_root: str) -> int:
    """Insert the seed bundle into decision_traces. Returns rows inserted."""
    inserted = 0
    for trace in _SEED_TRACES:
        trust_db.record_trace(
            repo_root=repo_root,
            session_id=SEED_SESSION_ID,
            task=trace.task,
            stage=trace.stage,
            action_type=trace.action_type,
            file_path=trace.file_path,
            change_type=trace.change_type,
            diff_size=trace.diff_size,
            blast_radius=trace.blast_radius,
            existing_lease=False,
            lease_type=None,
            prior_approvals=trace.prior_approvals,
            prior_denials=trace.prior_denials,
            policy_action=trace.policy_action,
            policy_score=trace.policy_score,
            user_decision=trace.user_decision,
            review_duration_seconds=trace.review_duration_seconds,
            rubber_stamp=trace.rubber_stamp,
            pushback_type=trace.pushback_type,
            policy_reasons=["seeded"],
            autonomy_mode="balanced",
        )
        inserted += 1
    return inserted


def prewarm_classifier(trust_db: TrustDB, repo_root: str) -> int:
    """Replay seeded traces through PolicyClassifier.update() so the learned
    scorer has crossed MIN_SAMPLES_FOR_LEARNED. Persists the updated pickle.

    Returns the number of update() calls applied.
    """
    classifier = trust_db.load_policy_model(repo_root) or build_cold_classifier()
    updates = 0
    for trace in _SEED_TRACES:
        # Construct a PolicyInput that matches the seeded action's risk shape.
        # Approvals/denials counts here reflect *prior* state at time-of-decision;
        # since the classifier doesn't read them as features per se but uses
        # change_pattern + risk + history, we feed the snapshot directly.
        pi = PolicyInput(
            prior_approvals=float(trace.prior_approvals),
            prior_denials=trace.prior_denials,
            avg_response_ms=int((trace.review_duration_seconds or 8.0) * 1000),
            avg_edit_distance=0.0,
            diff_size=trace.diff_size,
            blast_radius=trace.blast_radius,
            is_new_file=False,
            is_security_sensitive="auth" in trace.file_path,
            change_pattern=trace.change_type,
            recent_denials=trace.prior_denials,
            files_in_action=1,
        )
        approved = trace.user_decision in {"approve", "auto_approve"}
        classifier.update(pi, approved=approved, count_sample=True)
        updates += 1
    trust_db.save_policy_model(repo_root, classifier)
    return updates


# Pre-seeded hypothesis: developer narrows scope when tests are bundled in.
# Cited trace IDs are filled in at load-time after seed traces land.
_SCOPE_NARROW_HYPOTHESIS = {
    "driver": "scope_narrow_when_tests_bundled",
    "source": "seeded",
    "prompt": (
        "When you propose a multi-file edit that includes tests, this "
        "developer narrows scope to the service/store layer first and "
        "adds tests separately."
    ),
    "rationale": (
        "Three prior tasks (validate titles, normalize ingredients, favorites "
        "toggle) showed the same pattern: test_api.py edits were denied with "
        "scope_narrow pushback, then service.py-only edits were approved."
    ),
    "preference_json": (
        '{"trigger":{"stages":["apply"]},"condition":{},'
        '"action":"full_checkin",'
        '"scope":{"level":"path","path_globs":["**/tests/**"]},'
        '"lifecycle":{"provenance":"hypothesis"}}'
    ),
}


_SCOPE_CONSTRAINT_HYPOTHESIS = {
    "driver": "scope_constraint",
    "source": "llm_generated",
    "prompt": (
        "When you propose a multi-file change, this developer prefers to "
        "narrow scope and review one file at a time."
    ),
    "rationale": (
        "Across recent multi-file proposals, the developer has consistently "
        "pushed back with scope-narrow language and approved the smaller "
        "follow-up. The LLM noticer flagged this as a candidate pattern."
    ),
    "preference_json": (
        '{"trigger":{"min_blast_radius":2,"stages":["apply"]},"condition":{},'
        '"action":"full_checkin",'
        '"scope":{"level":"repo"},'
        '"lifecycle":{"provenance":"hypothesis"}}'
    ),
}


def preseed_hypothesis(trust_db: TrustDB, repo_root: str) -> int | None:
    """Insert two near-threshold hypothesis candidates so visitors see both
    pipelines on /prefs:
      1. A rule-based candidate (the original tests-bundled scope narrow).
      2. An LLM-noticed candidate (scope_constraint) that advances on the
         visitor's first scope-narrow pushback.
    Returns the id of the rule-based candidate (legacy contract)."""
    rule_id = trust_db.add_hypothesis_candidate(
        repo_root=repo_root,
        session_id=SEED_SESSION_ID,
        driver=_SCOPE_NARROW_HYPOTHESIS["driver"],
        source=_SCOPE_NARROW_HYPOTHESIS["source"],
        prompt=_SCOPE_NARROW_HYPOTHESIS["prompt"],
        rationale=_SCOPE_NARROW_HYPOTHESIS["rationale"],
        preference_json=_SCOPE_NARROW_HYPOTHESIS["preference_json"],
    )
    trust_db.update_hypothesis_evidence(rule_id, delta_for=2)

    llm_id = trust_db.add_hypothesis_candidate(
        repo_root=repo_root,
        session_id=SEED_SESSION_ID,
        driver=_SCOPE_CONSTRAINT_HYPOTHESIS["driver"],
        source=_SCOPE_CONSTRAINT_HYPOTHESIS["source"],
        prompt=_SCOPE_CONSTRAINT_HYPOTHESIS["prompt"],
        rationale=_SCOPE_CONSTRAINT_HYPOTHESIS["rationale"],
        preference_json=_SCOPE_CONSTRAINT_HYPOTHESIS["preference_json"],
    )
    trust_db.update_hypothesis_evidence(llm_id, delta_for=2)
    return rule_id


def seed_demo(trust_db: TrustDB, repo_root: str) -> dict:
    """Top-level entry: load traces, pre-warm classifier, pre-seed hypothesis.
    Idempotent — bails out if the repo already has seeded state."""
    if already_seeded(trust_db, repo_root):
        return {"already_seeded": True, "traces": 0, "updates": 0, "hypothesis_id": None}
    traces = load_seed_traces(trust_db, repo_root)
    updates = prewarm_classifier(trust_db, repo_root)
    hypothesis_id = preseed_hypothesis(trust_db, repo_root)
    return {
        "already_seeded": False,
        "traces": traces,
        "updates": updates,
        "hypothesis_id": hypothesis_id,
    }
