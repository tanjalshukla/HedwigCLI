#!/usr/bin/env python3
"""Seed the demo repo's trust DB with synthetic developer history.

Run this once after `hw init` and before the demo to pre-load enough
decisions so `hw observe weights` shows visible coefficient drift — making
the ML story tangible to a conference audience in one command.

Usage:
    python scripts/seed_demo_db.py [--repo-root PATH] [--n-sessions N] [--dry-run]

Defaults to the current working directory as the repo root.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Ensure the project root is importable regardless of where this is called from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sc.autonomy import AutonomyPreferences
from sc.ml_policy import build_warm_start_classifier, featurize
from sc.policy import PolicyInput
from sc.trust_db import TrustDB
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_policy_scenarios import PERSONA_CAUTIOUS, PERSONA_PERMISSIVE


# ---------------------------------------------------------------------------
# Synthetic history fixture
#
# Two files with opposite histories:
#   - api/routes.py:  repeatedly denied (API changes, slow deliberate reviews)
#   - utils/helpers.py: repeatedly approved (low-risk changes, fast reviews)
#
# Plus a security-sensitive file that always triggers check-in.
# ---------------------------------------------------------------------------

_SESSIONS: list[dict] = [
    # Session 1 — developer denies API routes, approves helpers
    {
        "file": "api/routes.py",
        "change_pattern": "api_change",
        "diff_size": 42,
        "blast_radius": 5,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 18_400,
        "approved": False,
    },
    {
        "file": "utils/helpers.py",
        "change_pattern": "general_change",
        "diff_size": 14,
        "blast_radius": 1,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 3_800,
        "approved": True,
    },
    # Session 2 — same pattern repeats
    {
        "file": "api/routes.py",
        "change_pattern": "api_change",
        "diff_size": 31,
        "blast_radius": 5,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 1,
        "recent_denials": 1,
        "response_ms": 22_100,
        "approved": False,
    },
    {
        "file": "utils/helpers.py",
        "change_pattern": "general_change",
        "diff_size": 9,
        "blast_radius": 1,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 1.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 4_100,
        "approved": True,
    },
    # Session 3 — third denial on routes, helpers now fast-tracked
    {
        "file": "api/routes.py",
        "change_pattern": "api_change",
        "diff_size": 55,
        "blast_radius": 5,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 2,
        "recent_denials": 2,
        "response_ms": 31_200,
        "approved": False,
    },
    {
        "file": "utils/helpers.py",
        "change_pattern": "general_change",
        "diff_size": 17,
        "blast_radius": 1,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 2.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 3_200,
        "approved": True,
    },
    # Session 4 — test files (low risk) approved quickly
    {
        "file": "tests/test_routes.py",
        "change_pattern": "test_generation",
        "diff_size": 60,
        "blast_radius": 1,
        "is_new_file": True,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 5_500,
        "approved": True,
    },
    {
        "file": "utils/helpers.py",
        "change_pattern": "general_change",
        "diff_size": 11,
        "blast_radius": 1,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 3.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 2_900,
        "approved": True,
    },
    # Session 5 — config change (medium risk) — approved after review
    {
        "file": "config/settings.py",
        "change_pattern": "config_change",
        "diff_size": 22,
        "blast_radius": 3,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 14_000,
        "approved": True,
    },
    {
        "file": "api/routes.py",
        "change_pattern": "api_change",
        "diff_size": 28,
        "blast_radius": 5,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 3,
        "recent_denials": 0,  # new session, no recent denials
        "response_ms": 27_500,
        "approved": False,
    },
    # Session 6 — data model change denied, docs approved quickly
    {
        "file": "models/user.py",
        "change_pattern": "data_model_change",
        "diff_size": 48,
        "blast_radius": 7,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 19_800,
        "approved": False,
    },
    {
        "file": "docs/api.md",
        "change_pattern": "documentation",
        "diff_size": 35,
        "blast_radius": 0,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 2_200,
        "approved": True,
    },
    # Session 7 — dependency update (medium risk) — approved after careful review
    {
        "file": "pyproject.toml",
        "change_pattern": "dependency_update",
        "diff_size": 8,
        "blast_radius": 2,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 0.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 11_500,
        "approved": True,
    },
    {
        "file": "utils/helpers.py",
        "change_pattern": "general_change",
        "diff_size": 20,
        "blast_radius": 1,
        "is_new_file": False,
        "is_security_sensitive": False,
        "prior_approvals": 4.0,
        "prior_denials": 0,
        "recent_denials": 0,
        "response_ms": 2_600,
        "approved": True,
    },
]


def _make_policy_input(row: dict) -> PolicyInput:
    return PolicyInput(
        prior_approvals=row["prior_approvals"],
        prior_denials=row["prior_denials"],
        avg_response_ms=float(row["response_ms"]),
        avg_edit_distance=0.1 if row["approved"] else 0.6,
        diff_size=row["diff_size"],
        blast_radius=row["blast_radius"],
        is_new_file=row["is_new_file"],
        is_security_sensitive=row["is_security_sensitive"],
        change_pattern=row["change_pattern"],
        recent_denials=row["recent_denials"],
        files_in_action=1,
        verification_failure_rate=None,
        model_confidence_avg=0.8 if row["approved"] else 0.3,
        model_confidence_samples=3,
    )


def seed(repo_root: Path, *, dry_run: bool = False, persona: str = "neutral", replay: int = 1) -> None:
    db_path = repo_root / ".sc" / "trust.db"
    if not db_path.exists():
        print(f"[error] No trust DB found at {db_path}. Run `hw init` first.")
        sys.exit(1)

    trust_db = TrustDB(db_path)
    repo_root_str = str(repo_root)

    # Always start from a fresh warm-start so the demo begins clean.
    trust_db.delete_policy_model(repo_root_str)
    classifier = build_warm_start_classifier()

    if persona == "cautious":
        base_decisions = [(pi, approved) for pi, approved in PERSONA_CAUTIOUS]
    elif persona == "permissive":
        base_decisions = [(pi, approved) for pi, approved in PERSONA_PERMISSIVE]
    else:
        base_decisions = [(_make_policy_input(row), row["approved"]) for row in _SESSIONS]

    decisions = base_decisions * replay

    print(f"Seeding {len(decisions)} developer decisions into {repo_root_str} (persona={persona}, replay={replay}x)")
    print(f"  warm-start sample_count = 0  (learning starts from heuristic priors)")

    for i, (pi, approved) in enumerate(decisions):
        label = "approve" if approved else "deny"
        if dry_run:
            score_before = classifier.score(pi)
            print(
                f"  [{i+1:2d}]  {label:6s}  score_before={score_before:.3f}"
            )
        classifier.update(pi, approved=approved)

    print(f"\n  sample_count after seeding = {classifier.sample_count}")
    print(f"  model {'ACTIVE (>= 10 decisions)' if classifier.ready() else 'NOT YET ACTIVE (< 10 decisions)'}")

    # Derive autonomy preferences from approval rate, simulating what Hedwig
    # would infer from long-term conversational cues with this developer.
    # Permissive (>=90% approval): trust the agent, skip low-risk plan gates.
    # Cautious (<=60% approval): no bypass granted.
    n_total = len(decisions)
    n_approvals = sum(1 for _, approved in decisions if approved)
    approval_rate = n_approvals / n_total if n_total else 0.0

    inferred_prefs = AutonomyPreferences()
    if approval_rate >= 0.90:
        inferred_prefs = AutonomyPreferences(
            prefer_fewer_checkins=True,
            skip_low_risk_plan_checkpoint=True,
        )
        print(f"  approval rate = {approval_rate:.0%} >= 90%, inferring autonomy preferences:")
        print(f"    prefer_fewer_checkins=True, skip_low_risk_plan_checkpoint=True")
    else:
        print(f"  approval rate = {approval_rate:.0%}, no autonomy bypass preferences set")

    if dry_run:
        print("\n[dry-run] No changes written.")
        return

    trust_db.save_policy_model(repo_root_str, classifier)
    if inferred_prefs.prefer_fewer_checkins or inferred_prefs.skip_low_risk_plan_checkpoint:
        trust_db.merge_autonomy_preferences(repo_root_str, inferred_prefs)

    # Print a preview of what `hw observe weights` will show.
    from sc.ml_policy import FEATURE_NAMES
    deltas = classifier.coef_delta()
    print("\n  Coefficient drift (visible via `hw observe weights`):")
    for name in FEATURE_NAMES:
        delta = deltas[name]
        if abs(delta) >= 0.02:
            direction = "+" if delta > 0 else "-"
            print(f"    {direction} {name:<30s}  delta={delta:+.4f}")

    print("\nDone. Run `hw observe weights` during the demo to show personalization.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Path to the repo root (default: current directory).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be seeded without writing to the DB.",
    )
    parser.add_argument(
        "--persona",
        default="neutral",
        choices=["neutral", "cautious", "permissive"],
        help="Developer persona history to seed (default: neutral).",
    )
    parser.add_argument(
        "--replay",
        type=int,
        default=1,
        help="Number of times to replay the persona decision list (default: 1). Replay=3 gives 45 effective decisions.",
    )
    args = parser.parse_args()
    seed(Path(args.repo_root).resolve(), dry_run=args.dry_run, persona=args.persona, replay=args.replay)


if __name__ == "__main__":
    main()
