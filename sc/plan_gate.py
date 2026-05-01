from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .autonomy import AutonomyPreferences
from .features import estimate_blast_radius, is_security_sensitive
from .schema import IntentDeclaration
from .trust_db import TrustDB


def _file_preview(files: list[str]) -> str:
    preview = ", ".join(files[:3])
    if len(files) > 3:
        preview += ", ..."
    return preview


@dataclass(frozen=True)
class PlanCheckpointDecision:
    required: bool
    reasons: tuple[str, ...]


def decide_plan_checkpoint(
    *,
    trust_db: TrustDB,
    repo_root: str,
    declaration: IntentDeclaration,
    strict: bool,
    max_auto_files: int,
    autonomy_preferences: AutonomyPreferences | None = None,
    repo_root_path: Path | None = None,
    spec_required: bool = False,
) -> PlanCheckpointDecision:
    """Decide whether implementation must pause for explicit plan approval."""
    reasons: list[str] = []
    planned_files = declaration.planned_files

    if strict:
        reasons.append("strict plan gate enabled")

    scope_reason = len(planned_files) > max(max_auto_files, 0)
    if scope_reason:
        reasons.append(f"plan touches {len(planned_files)} files")

    # Running tests is expected post-edit hygiene and should not by itself force
    # a plan checkpoint on low-risk, trusted edits.
    material_actions = {action for action in declaration.planned_actions if action != "run_tests"}
    multi_action_reason = len(material_actions) > 1
    if multi_action_reason:
        reasons.append("plan includes multiple action types")
    if declaration.potential_deviations:
        reasons.append("plan anticipates possible deviations")
    if spec_required and not declaration.requirements_covered:
        reasons.append("spec provided but plan does not map work to requirements")

    low_trust_files: list[str] = []
    constrained_files: list[str] = []
    security_files: list[str] = []
    high_blast_files: list[str] = []
    for path in planned_files:
        history = trust_db.policy_history(repo_root, path, stage="apply")
        if history.denials > 0 and history.approvals <= history.denials:
            low_trust_files.append(path)

        constraint = trust_db.strongest_constraint(repo_root, path, access_type="write")
        if constraint and constraint.policy_for("write") in {"always_check_in", "always_deny"}:
            constrained_files.append(path)

        if is_security_sensitive(path, ""):
            security_files.append(path)

        if repo_root_path is not None:
            try:
                blast = estimate_blast_radius(repo_root_path, path)
            except Exception:
                blast = 1
            if blast > 5:
                high_blast_files.append(path)

    low_trust_reason = bool(low_trust_files)
    if low_trust_reason:
        reasons.append(f"low-trust files: {_file_preview(low_trust_files)}")

    constrained_reason = bool(constrained_files)
    if constrained_reason:
        reasons.append(f"constrained files: {_file_preview(constrained_files)}")

    security_reason = bool(security_files)
    if security_reason:
        reasons.append(f"security-sensitive paths: {_file_preview(security_files)}")

    verification_risk_files: list[str] = []
    for path in planned_files:
        failure_rate = trust_db.verification_failure_rate(repo_root, path, stage="apply")
        if failure_rate is not None and failure_rate >= 0.34:
            verification_risk_files.append(path)
    if verification_risk_files:
        reasons.append(f"recent verification failures in area: {_file_preview(verification_risk_files)}")

    high_blast_reason = bool(high_blast_files)
    if high_blast_reason:
        reasons.append(f"high import count / blast radius: {_file_preview(high_blast_files)}")

    if declaration.workflow_phase == "research":
        reasons.append("declared phase is research")
    elif declaration.workflow_phase == "planning" and len(planned_files) > 1:
        reasons.append("declared phase is planning with multi-file scope")

    if autonomy_preferences and autonomy_preferences.skip_low_risk_plan_checkpoint:
        # Hard-risk signals always block the bypass (security, constrained files,
        # low-trust history, explicit strict mode, multi-action plans).
        hard_risk = any(
            (
                strict,
                multi_action_reason,
                low_trust_reason,
                constrained_reason,
                security_reason,
                high_blast_reason,
                spec_required and not declaration.requirements_covered,
                declaration.workflow_phase == "research",
            )
        )
        # Soft-risk signals (potential_deviations) can be bypassed for permissive
        # developers who have explicitly requested fewer check-ins, since these
        # reflect model uncertainty rather than deterministic risk.
        if not hard_risk and scope_reason and len(planned_files) <= max(max_auto_files, 0) + 1:
            return PlanCheckpointDecision(required=False, reasons=tuple())

    return PlanCheckpointDecision(required=bool(reasons), reasons=tuple(reasons))
