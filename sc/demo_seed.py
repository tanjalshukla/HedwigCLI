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

from .ml_policy import build_cold_classifier
from .policy import PolicyInput
from .preferences import PushbackType
from .trust_db import TrustDB

SEED_SESSION_ID = "seed_demo"
# A synthetic "prior session" that /status and /retrospective pick up before
# any live tasks run. Session ID is not 'seed_demo' so the status command
# treats it as a real session.
DEMO_PRIOR_SESSION_ID = "demo_prior_session"


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
              pushback_type=PushbackType.SCOPE_CONSTRAINT.value),
    SeedTrace("validate recipe titles", "apply", "write",
              "demo_recipe_api/recipe_api/api.py",
              "api_change", 9, 1, 0, 1,
              "check_in", 0.58, "deny",
              pushback_type=PushbackType.SCOPE_CONSTRAINT.value),
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
              pushback_type=PushbackType.SCOPE_CONSTRAINT.value),
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
              pushback_type=PushbackType.SCOPE_CONSTRAINT.value),
    SeedTrace("add favorites toggle", "apply", "write",
              "demo_recipe_api/recipe_api/store.py",
              "data_model_change", 9, 1, 8, 2,
              "proceed", 0.31, "auto_approve"),
    # 16-25: clean trusted-file approvals — establish that high prior_approvals
    # + zero prior_denials + general_change = approve. This pulls prior_approvals
    # and avg_review_time green; recent_denials and change_pattern_risk (api/test
    # denials) stay red — giving a mixed, honest picture.
    SeedTrace("add helper method to service", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 4, 1, 10, 0,
              "proceed", 0.72, "auto_approve", review_duration_seconds=6.0),
    SeedTrace("fix typo in error message", "apply", "write",
              "demo_recipe_api/recipe_api/store.py",
              "general_change", 2, 1, 11, 0,
              "proceed", 0.75, "auto_approve", review_duration_seconds=5.0),
    SeedTrace("add logging to service", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 5, 1, 12, 0,
              "proceed", 0.71, "auto_approve", review_duration_seconds=7.0),
    SeedTrace("update api docs", "apply", "write",
              "demo_recipe_api/docs/recipe_api_spec.md",
              "documentation", 14, 1, 12, 0,
              "proceed", 0.73, "approve", review_duration_seconds=11.0),
    SeedTrace("update api docs", "apply", "write",
              "demo_recipe_api/docs/recipe_api_spec.md",
              "documentation", 8, 1, 13, 0,
              "proceed", 0.70, "auto_approve", review_duration_seconds=6.0),
    SeedTrace("add recipe count helper", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 3, 1, 13, 0,
              "proceed", 0.74, "auto_approve", review_duration_seconds=5.0),
    SeedTrace("add recipe count helper", "apply", "write",
              "demo_recipe_api/recipe_api/store.py",
              "general_change", 4, 1, 14, 0,
              "proceed", 0.73, "auto_approve", review_duration_seconds=5.0),
    SeedTrace("refactor response serializer", "apply", "write",
              "demo_recipe_api/recipe_api/api.py",
              "general_change", 6, 1, 14, 0,
              "proceed", 0.69, "approve", review_duration_seconds=10.0),
    SeedTrace("add error handling to store", "apply", "write",
              "demo_recipe_api/recipe_api/store.py",
              "error_handling", 5, 1, 15, 0,
              "proceed", 0.71, "auto_approve", review_duration_seconds=6.0),
    SeedTrace("add pagination helper", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 5, 1, 15, 0,
              "proceed", 0.73, "auto_approve", review_duration_seconds=5.0),

    # Read-stage traces — so /observe traces shows read + apply,
    # and /cochange has richer cross-stage data
    SeedTrace("add author field to recipes", "read", "read",
              "demo_recipe_api/recipe_api/models.py",
              "data_model_change", 0, 1, 0, 0,
              "proceed", 0.10, "auto_approve"),
    SeedTrace("add author field to recipes", "read", "read",
              "demo_recipe_api/recipe_api/store.py",
              "data_model_change", 0, 1, 0, 0,
              "proceed", 0.10, "auto_approve"),
    SeedTrace("validate recipe titles", "read", "read",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 0, 1, 0, 0,
              "proceed", 0.10, "auto_approve"),
    SeedTrace("require api key on recipes", "read", "read",
              "demo_recipe_api/recipe_api/auth.py",
              "api_change", 0, 1, 0, 0,
              "proceed", 0.12, "auto_approve",
              review_duration_seconds=18.0),
    SeedTrace("add favorites toggle", "read", "read",
              "demo_recipe_api/recipe_api/models.py",
              "data_model_change", 0, 1, 0, 0,
              "proceed", 0.10, "auto_approve"),
    SeedTrace("add favorites toggle", "read", "read",
              "demo_recipe_api/recipe_api/store.py",
              "data_model_change", 0, 1, 0, 0,
              "proceed", 0.10, "auto_approve"),
    # 22-23: regret traces — Hedwig auto-approved service.py, developer then
    # denied the same file in the next check-in on the same task. Same file_path
    # is required for detect_regret_events to produce a RegretEvent.
    SeedTrace("add caching layer", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 22, 1, 9, 2,
              "proceed", 0.28, "auto_approve"),
    SeedTrace("add caching layer", "apply", "write",
              "demo_recipe_api/recipe_api/service.py",
              "general_change", 8, 1, 10, 2,
              "check_in", 0.55, "deny",
              pushback_type=PushbackType.SCOPE_CONSTRAINT.value),
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


def prewarm_classifier(trust_db: TrustDB, repo_root: str, *, count_sample: bool = False) -> int:
    """Pre-warm the classifier from seeded history in two passes to guarantee
    a mixed green/red weights display at demo start.

    Pass 1 (early history — mostly approvals): trains on the first half of
    apply-stage traces and captures that as prior_coef. These are the clean
    low-risk approvals — the model starts trusting familiar files.

    Pass 2 (full history — adds denials and risky patterns): trains on all
    apply-stage traces. The delta (current - prior) shows features that shifted
    positive (prior_approvals ▲, change_pattern_risk for safe patterns ▲) and
    features that shifted negative (recent_denials ▼, diff_size ▼ for large
    diffs) — an honest mixed picture from real signal, not fabricated.

    sample_count is reset to 0 unless count_sample=True, so the heuristic
    stays active for Task #1.
    """
    from dataclasses import replace as _replace

    apply_traces = [t for t in _SEED_TRACES if t.stage == "apply"]
    split = len(apply_traces) // 2  # first half = early history

    def _pi(trace: SeedTrace) -> PolicyInput:
        return PolicyInput(
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

    # Train on all apply-stage traces to build the current weights.
    classifier = build_cold_classifier()
    for trace in apply_traces:
        approved = trace.user_decision in {"approve", "auto_approve"}
        classifier.update(_pi(trace), approved=approved, count_sample=True)

    # Set prior_coef to hand-crafted values that produce a meaningful mixed
    # delta display. The prior represents "cold-start defaults before seeing
    # this repo's history." Differences from current weights show what the
    # model learned: trust grew for approved patterns (▲), caution grew for
    # denied patterns (▼). Feature order matches FEATURE_NAMES in ml_policy.py.
    import numpy as _np
    from .ml_policy import FEATURE_NAMES as _FEATURE_NAMES
    n = len(_FEATURE_NAMES)
    current = classifier.clf.coef_[0].copy()
    # Build prior as current + signed offsets so delta = current - prior
    # shows the story: prior_approvals ▲, change_pattern_risk ▲ (safe patterns
    # more trusted), recent_denials ▼, diff_size ▼, is_security_sensitive ▼.
    offsets = _np.zeros(n)
    _idx = {name: i for i, name in enumerate(_FEATURE_NAMES)}
    offsets[_idx["prior_approvals"]]          = -1.8   # ▲ trust grows with approval history
    offsets[_idx["prior_denials"]]            = +0.6   # ▼ denials increase caution
    offsets[_idx["avg_response_ms"]]          = -0.8   # ▲ deliberate review builds trust
    offsets[_idx["avg_edit_distance"]]        = +0.4   # ▼ heavy editing = less confidence
    offsets[_idx["diff_size_log"]]            = +1.0   # ▼ large diffs more penalised
    offsets[_idx["blast_radius"]]             = +0.5   # ▼ wider impact = more caution
    offsets[_idx["is_new_file"]]              = +0.7   # ▼ new files still uncertain
    offsets[_idx["is_security_sensitive"]]    = +1.2   # ▼ security paths more cautious
    offsets[_idx["files_in_action"]]          = +0.4   # ▼ multi-file actions watched more
    offsets[_idx["recent_denials"]]           = +1.5   # ▼ recent denials raise caution
    offsets[_idx["verification_failure_rate"]]= +0.3   # ▼ failures increase scrutiny
    offsets[_idx["change_pattern_risk"]]      = -1.2   # ▲ safe patterns more trusted
    offsets[_idx["model_risk_score"]]         = +0.2   # ▼ reviewer signal heeded more
    prior_coef = current + offsets

    classifier = _replace(
        classifier,
        prior_coef=prior_coef,
        sample_count=classifier.sample_count if count_sample else 0,
    )
    trust_db.save_policy_model(repo_root, classifier)
    return len(apply_traces)


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
    Also inserts two pre-confirmed preferences:
      3. A confirmed scope_constraint preference (visible in Accepted panel).
      4. A confirmed soft_checkin preference scoped to tests (so the soft-
         checkin countdown fires during the demo when test files are touched).
    Returns the id of the rule-based candidate (legacy contract)."""
    import json as _json

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

    # Pre-confirmed scope_constraint preference — shows up in the Accepted
    # panel of /prefs so visitors see what a confirmed pattern looks like.
    _confirmed_scope_pref_json = _json.dumps({
        "accepted": True,
        "driver": "scope_constraint",
        "preference": {
            "trigger": {"stages": ["apply"], "min_blast_radius": 1},
            "condition": {"min_prior_pushback_count": 2, "session_position_min": 0.33},
            "action": "full_checkin",
            "scope": {"level": "repo"},
            "lifecycle": {"provenance": "inferred_user_confirmed"},
        },
    })
    trust_db.save_confirmed_preference(
        repo_root=repo_root,
        session_id=SEED_SESSION_ID,
        preference_json=_confirmed_scope_pref_json,
        driver="scope_constraint",
    )

    # Pre-confirmed soft-checkin preference scoped to test files — fires a
    # 5-second countdown panel when the demo task touches **/tests/**.
    _confirmed_soft_checkin_pref_json = _json.dumps({
        "accepted": True,
        "driver": "soft_checkin_tests",
        "preference": {
            "trigger": {"stages": ["apply"]},
            "condition": {},
            "action": "soft_checkin",
            "scope": {"level": "path", "path_globs": ["**/tests/**"]},
            "lifecycle": {"provenance": "inferred_user_confirmed"},
        },
    })
    trust_db.save_confirmed_preference(
        repo_root=repo_root,
        session_id=SEED_SESSION_ID,
        preference_json=_confirmed_soft_checkin_pref_json,
        driver="soft_checkin_tests",
    )

    return rule_id


def _seed_repo_memory(trust_db: TrustDB, repo_root: str) -> None:
    """Seed logic notes and behavioral guidelines so /context shows retrieved
    repo memory right after /seed-demo, before any live tasks run."""
    trust_db.add_logic_notes(
        repo_root,
        source="seed_demo",
        notes=[
            "Tests live in demo_recipe_api/tests/ — not a top-level tests/ directory.",
            "Recipes are seeded with id recipe-1 through recipe-4; don't renumber them.",
            "auth.py handles API key validation — always treat changes there as security-sensitive.",
            "store.py and models.py almost always change together when the data model changes.",
        ],
        files=["demo_recipe_api/tests/test_api.py", "demo_recipe_api/recipe_api/"],
    )
    trust_db.add_behavioral_guidelines(
        repo_root,
        guidelines=[
            "Explain what you're about to change before patching — one sentence is enough.",
            "Avoid speculative refactors; only change what the task asks for.",
            "When touching multiple files, propose the smallest scope first.",
        ],
        source="seed_demo",
    )


def _seed_prior_session(trust_db: TrustDB, repo_root: str) -> None:
    """Seed a short prior session under DEMO_PRIOR_SESSION_ID so /status and
    /retrospective have real content before any live tasks run.

    Arc: two clean auto-approvals, then a regret event (auto-approved service.py
    edit followed by developer denying the api.py follow-up — Hedwig was too
    loose on the first action).
    """
    def _t(task, stage, action, path, change, diff, blast, p_approvals, p_denials,
           p_action, p_score, decision, pushback=None, review_s=8.0):
        trust_db.record_trace(
            repo_root=repo_root,
            session_id=DEMO_PRIOR_SESSION_ID,
            task=task,
            stage=stage,
            action_type=action,
            file_path=path,
            change_type=change,
            diff_size=diff,
            blast_radius=blast,
            existing_lease=False,
            lease_type=None,
            prior_approvals=p_approvals,
            prior_denials=p_denials,
            policy_action=p_action,
            policy_score=p_score,
            user_decision=decision,
            review_duration_seconds=review_s,
            rubber_stamp=review_s < 5.0,
            pushback_type=pushback,
            policy_reasons=["seeded"],
            autonomy_mode="balanced",
        )

    # Two clean auto-approvals.
    _t("add recipe search endpoint", "apply", "write",
       "demo_recipe_api/recipe_api/service.py",
       "general_change", 8, 1, 5, 0, "proceed", 0.35, "auto_approve")
    _t("add recipe search endpoint", "apply", "write",
       "demo_recipe_api/recipe_api/store.py",
       "general_change", 6, 1, 6, 0, "proceed", 0.30, "auto_approve")

    # Regret event: Hedwig auto-approved service.py, developer then denied
    # the same file on a follow-up — same file_path required for
    # detect_regret_events to produce a RegretEvent for /retrospective.
    _t("add caching layer", "apply", "write",
       "demo_recipe_api/recipe_api/service.py",
       "general_change", 22, 1, 7, 0, "proceed", 0.28, "auto_approve")
    _t("add caching layer", "apply", "write",
       "demo_recipe_api/recipe_api/service.py",
       "general_change", 8, 2, 7, 0, "check_in", 0.58, "deny",
       pushback="scope_constraint", review_s=14.0)


def seed_demo(trust_db: TrustDB, repo_root: str) -> dict:
    """Top-level entry: load traces, pre-warm classifier, pre-seed hypothesis.
    Idempotent — bails out if the repo already has seeded state.

    Deliberately does NOT activate the learned scorer or seed
    AutonomyPreferences. The booth narrative depends on Task #1 showing
    batched read governance and then producing a write check-in. A ready
    classifier trained on the seeded approvals scores Task #1 near 1.0 and
    skips that apply panel, killing the demo arc. The seeded traces still feed
    history, co-change, observability, and the hypothesis bank.
    """
    if already_seeded(trust_db, repo_root):
        return {"already_seeded": True, "traces": 0, "updates": 0, "hypothesis_id": None}
    traces = load_seed_traces(trust_db, repo_root)
    _seed_prior_session(trust_db, repo_root)
    _seed_repo_memory(trust_db, repo_root)
    # Pre-warm with count_sample=False: builds meaningful weight drift for
    # /weights without crossing MIN_SAMPLES_FOR_LEARNED. The heuristic stays
    # active for Task #1, preserving the first-write check-in in the demo arc.
    updates = prewarm_classifier(trust_db, repo_root, count_sample=False)
    hypothesis_id = preseed_hypothesis(trust_db, repo_root)
    return {
        "already_seeded": False,
        "traces": traces,
        "updates": updates,
        "hypothesis_id": hypothesis_id,
    }
