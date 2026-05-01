from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import typer
from rich import print
from rich.table import Table

from ..autonomy import adjusted_policy_thresholds
from ..commands.shared import open_trust_db, require_repo_root
from ..cli_shared import is_approval_decision as _is_approval_decision
from ..config import load_config

def _format_expiry(expires_at: int | None) -> str:
    if expires_at is None:
        return "permanent"
    now = int(time.time())
    delta = expires_at - now
    if delta <= 0:
        return "expired"
    minutes = delta // 60
    hours = minutes // 60
    days = hours // 24
    if days > 0:
        return f"in {days}d {hours % 24}h"
    if hours > 0:
        return f"in {hours}h {minutes % 60}m"
    return f"in {minutes}m"


def _format_timestamp(epoch_seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch_seconds))


def _truncate_text(value: str | None, *, max_len: int) -> str:
    if not value:
        return "-"
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _format_verify_cell(value: int | None) -> str:
    if value == 1:
        return "pass"
    if value == 0:
        return "fail"
    return "-"


def _format_trace_row(row: dict) -> list[str]:
    return [
        str(row["id"]),
        _format_timestamp(int(row["created_at"])),
        row["stage"],
        row["file_path"],
        row["check_in_initiator"] or "-",
        (
            f"{float(row['model_confidence_self_report']):.2f}"
            if row["model_confidence_self_report"] is not None
            else "-"
        ),
        f"{row['policy_action']} ({row['policy_score']:.2f})",
        row["user_decision"],
        _truncate_text(row["user_feedback_text"], max_len=40),
        _format_verify_cell(row["verification_passed"]),
        str(row["diff_size"] if row["diff_size"] is not None else "-"),
        (
            f"{float(row['review_duration_seconds']):.1f}"
            if row["review_duration_seconds"] is not None
            else "-"
        ),
        "quick"
        if row["rubber_stamp"] == 1 and _is_approval_decision(str(row["user_decision"]))
        else "-",
        str(row["response_time_ms"] if row["response_time_ms"] is not None else "-"),
    ]


def leases(
    json_out: bool = typer.Option(False, "--json", help="Output leases as JSON."),
):
    """List active leases for this repo."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    leases = trust_db.list_active_leases(str(repo_root))
    if not leases:
        print("[yellow]No active leases.[/yellow]")
        return

    if json_out:
        payload = [
            {
                "file_path": lease.file_path,
                "expires_at": lease.expires_at,
                "type": lease.lease_type,
            }
            for lease in leases
        ]
        print(json.dumps(payload, indent=2))
        return

    table = Table(title="Active Leases")
    table.add_column("Type")
    table.add_column("File")
    table.add_column("Expires")
    for lease in leases:
        table.add_row(lease.lease_type, lease.file_path, _format_expiry(lease.expires_at))
    print(table)


def traces(
    limit: int = typer.Option(30, "--limit", help="Number of recent trace rows to show."),
    json_out: bool = typer.Option(False, "--json", help="Output traces as JSON."),
):
    """List recent governance traces for this repo."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    rows = trust_db.list_traces(str(repo_root), limit=limit)
    if not rows:
        print("[yellow]No traces recorded yet.[/yellow]")
        return

    if json_out:
        payload = [dict(row) for row in rows]
        print(json.dumps(payload, indent=2))
        return

    columns = [
        "ID",
        "Time",
        "Stage",
        "File",
        "Initiator",
        "MConf",
        "Policy",
        "Decision",
        "Feedback",
        "Verify",
        "Diff",
        "Rev(s)",
        "Review",
        "Resp(ms)",
    ]
    table = Table(title="Recent Traces")
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*_format_trace_row(dict(row)))
    print(table)


def explain(
    trace_id: int = typer.Argument(..., help="Trace row id from `hw observe traces`."),
):
    """Explain why a specific decision happened using stored trace context."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    row = trust_db.trace_by_id(str(repo_root), trace_id)
    if row is None:
        print(f"[red]Trace id {trace_id} not found for this repo.[/red]")
        raise typer.Exit(code=1)

    reasons: list[str] = []
    if row["policy_reasons_json"]:
        try:
            parsed = json.loads(row["policy_reasons_json"])
            reasons = [str(item) for item in parsed if str(item).strip()]
        except Exception:
            reasons = []

    assumptions: list[str] = []
    if row["model_assumptions_json"]:
        try:
            parsed = json.loads(row["model_assumptions_json"])
            assumptions = [str(item) for item in parsed if str(item).strip()]
        except Exception:
            assumptions = []

    print(f"[bold]Trace {trace_id}[/bold]")
    print(f"Time: {_format_timestamp(int(row['created_at']))}")
    print(f"Stage/action: {row['stage']} / {row['action_type']}")
    print(f"File: {row['file_path']}")
    print(f"Policy decision: {row['policy_action']} (score={row['policy_score']:.2f})")
    print(f"User decision: {row['user_decision']}")
    print(
        "Context: "
        f"prior approvals={row['prior_approvals']}, "
        f"prior denials={row['prior_denials']}, "
        f"existing lease={'yes' if row['existing_lease'] else 'no'}"
    )
    if row["diff_size"] is not None or row["blast_radius"] is not None:
        print(
            "Risk: "
            f"diff_size={row['diff_size'] if row['diff_size'] is not None else '-'}, "
            f"blast_radius={row['blast_radius'] if row['blast_radius'] is not None else '-'}, "
            f"change_type={row['change_type'] or '-'}"
        )
    if reasons:
        print("Policy reasons:")
        for reason in reasons:
            print(f"  - {reason}")
    if row["check_in_initiator"]:
        print(f"Check-in initiator: {row['check_in_initiator']}")
    if row["review_duration_seconds"] is not None:
        duration = float(row["review_duration_seconds"])
        label = " (quick approval)" if row["rubber_stamp"] == 1 and _is_approval_decision(str(row["user_decision"])) else ""
        print(f"Review duration: {duration:.2f}s{label}")
    if row["model_confidence_self_report"] is not None:
        print(f"Model confidence: {float(row['model_confidence_self_report']):.2f}")
    if assumptions:
        print("Model assumptions:")
        for assumption in assumptions:
            print(f"  - {assumption}")
    if row["user_feedback_text"]:
        print(f"Developer feedback: {row['user_feedback_text']}")
    if row["verification_passed"] is not None:
        state = "pass" if row["verification_passed"] == 1 else "fail"
        print(f"Verification: {state}")
    if row["expected_behavior"]:
        print(f"Expected behavior: {row['expected_behavior']}")


def checkin_stats(
    json_out: bool = typer.Option(False, "--json", help="Output check-in calibration stats as JSON."),
):
    """Show check-in calibration metrics grouped by initiator and stage."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    rows = trust_db.checkin_calibration(str(repo_root))
    if not rows:
        print("[yellow]No check-in calibration data yet.[/yellow]")
        return

    if json_out:
        payload = [
            {
                "initiator": row.initiator,
                "stage": row.stage,
                "total": row.total,
                "approvals": row.approvals,
                "denials": row.denials,
                "approval_rate": row.approval_rate,
                "avg_response_ms": row.avg_response_ms,
            }
            for row in rows
        ]
        print(json.dumps(payload, indent=2))
        return

    table = Table(title="Check-in Calibration")
    table.add_column("Initiator")
    table.add_column("Stage")
    table.add_column("Total")
    table.add_column("Approvals")
    table.add_column("Denials")
    table.add_column("Approval %")
    table.add_column("Avg Resp(ms)")
    for row in rows:
        table.add_row(
            row.initiator,
            row.stage,
            str(row.total),
            str(row.approvals),
            str(row.denials),
            f"{row.approval_rate * 100:.1f}",
            f"{row.avg_response_ms:.0f}" if row.avg_response_ms is not None else "-",
        )
    print(table)


def preferences(
    json_out: bool = typer.Option(False, "--json", help="Output learned autonomy preferences as JSON."),
):
    """Show currently learned autonomy preferences and active scoring band thresholds."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)
    prefs = trust_db.autonomy_preferences(repo_root_str)

    config = load_config(repo_root)
    base_proceed = getattr(config, "proceed_threshold", 0.9) if config else 0.9
    base_flag = getattr(config, "flag_threshold", 0.2) if config else 0.2
    model_total, model_approval_rate = trust_db.model_checkin_calibration(repo_root_str)
    adj_proceed, adj_flag = adjusted_policy_thresholds(
        base_proceed,
        base_flag,
        prefs,
        model_checkin_approval_rate=model_approval_rate,
        model_checkin_total=model_total,
    )

    payload = {
        "prefer_fewer_checkins": prefs.prefer_fewer_checkins,
        "allowed_checkin_topics": list(prefs.allowed_checkin_topics),
        "skip_low_risk_plan_checkpoint": prefs.skip_low_risk_plan_checkpoint,
        "scoped_paths": list(prefs.scoped_paths),
        "effective_thresholds": {
            "proceed": round(adj_proceed, 3),
            "flag": round(adj_flag, 3),
            "bands": {
                f"score >= {adj_proceed:.2f}": "auto-approve (silent)",
                f"score >= {adj_flag:.2f} and < {adj_proceed:.2f}": "auto-approve (flagged for summary)",
                f"score < {adj_flag:.2f}": "check-in required",
            },
        },
    }

    if json_out:
        print(json.dumps(payload, indent=2))
        return

    table = Table(title="Autonomy Preferences")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Prefer fewer check-ins", "yes" if prefs.prefer_fewer_checkins else "no")
    table.add_row(
        "Allowed check-in topics",
        ", ".join(prefs.allowed_checkin_topics) if prefs.allowed_checkin_topics else "-",
    )
    table.add_row(
        "Skip low-risk plan checkpoints",
        "yes" if prefs.skip_low_risk_plan_checkpoint else "no",
    )
    table.add_row("Scoped paths", ", ".join(prefs.scoped_paths) if prefs.scoped_paths else "-")
    print(table)

    # Show the effective scoring bands so the developer can see how preferences
    # have shifted the thresholds. Gray-area behavior: the three bands are:
    #   proceed     — auto-approve silently (score above proceed threshold)
    #   proceed_flag — auto-approve but flag in summary (between thresholds)
    #   check_in    — always ask the developer (score below flag threshold)
    threshold_table = Table(title="Effective Scoring Bands (heuristic priors, see SPEC.md)")
    threshold_table.add_column("Score Range")
    threshold_table.add_column("Action")
    threshold_table.add_row(f">= {adj_proceed:.2f}", "auto-approve (silent)")
    threshold_table.add_row(
        f">= {adj_flag:.2f} and < {adj_proceed:.2f}",
        "auto-approve (flagged for summary)",
    )
    threshold_table.add_row(f"< {adj_flag:.2f}", "check-in required")
    print(threshold_table)
    print(
        "[dim]To revoke a preference: hw observe preferences-revoke --fewer-checkins "
        "| --topic <name> | --path <path>[/dim]"
    )


def preferences_revoke(
    topics: list[str] = typer.Option(
        None,
        "--topic",
        help=(
            "Check-in topic to remove from allowed_checkin_topics "
            "(api, signature, schema, security, architecture, config, test, deployment). "
            "Repeat to remove multiple."
        ),
    ),
    paths: list[str] = typer.Option(
        None,
        "--path",
        help="Scoped path to remove from scoped_paths. Repeat to remove multiple.",
    ),
    fewer_checkins: bool = typer.Option(
        False,
        "--fewer-checkins/--no-fewer-checkins",
        help="Reset the prefer-fewer-check-ins flag to False.",
    ),
    skip_plan: bool = typer.Option(
        False,
        "--skip-plan-checkpoint/--no-skip-plan-checkpoint",
        help="Reset the skip-low-risk-plan-checkpoint flag to False.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
):
    """Revoke specific learned autonomy preferences without resetting everything.

    Examples:

      hw observe preferences-revoke --fewer-checkins
      hw observe preferences-revoke --topic api --topic schema
      hw observe preferences-revoke --path src/models
    """
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)

    # Show current state first so the user knows what will change.
    prefs = trust_db.autonomy_preferences(repo_root_str)
    nothing_to_revoke = (
        not (fewer_checkins and prefs.prefer_fewer_checkins)
        and not (skip_plan and prefs.skip_low_risk_plan_checkpoint)
        and not (topics and set(topics) & set(prefs.allowed_checkin_topics))
        and not (paths and set(paths) & set(prefs.scoped_paths))
    )
    if nothing_to_revoke:
        print("[yellow]No matching preferences to revoke.[/yellow]")
        return

    if not yes:
        from rich.prompt import Prompt
        confirmed = Prompt.ask("Revoke these preferences?", choices=["y", "n"], default="y")
        if confirmed != "y":
            print("[yellow]No changes made.[/yellow]")
            raise typer.Exit(code=0)

    revoked = trust_db.revoke_autonomy_preference(
        repo_root_str,
        topics=tuple(topics or []),
        paths=tuple(paths or []),
        prefer_fewer_checkins=fewer_checkins,
        skip_low_risk_plan_checkpoint=skip_plan,
    )
    if revoked:
        for item in revoked:
            print(f"[green]{item}[/green]")
    else:
        print("[yellow]No changes made.[/yellow]")


def preferences_clear(
    yes: bool = typer.Option(False, "--yes", help="Confirm deleting learned autonomy preferences."),
):
    """Delete learned autonomy preferences for this repo."""
    if not yes:
        print("[red]Refusing to clear preferences without --yes.[/red]")
        raise typer.Exit(code=1)
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    removed = trust_db.delete_autonomy_preferences(str(repo_root))
    if removed:
        print("[green]Cleared learned autonomy preferences.[/green]")
    else:
        print("[yellow]No learned autonomy preferences found.[/yellow]")


def weights():
    """Show learned policy coefficients vs. heuristic priors and real sample count."""
    from ..ml_policy import FEATURE_NAMES

    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    classifier = trust_db.load_policy_model(str(repo_root))

    if classifier is None:
        print("[yellow]No learned model found for this repo. Run `hw init` first.[/yellow]")
        raise typer.Exit(code=0)

    real_samples = classifier.sample_count
    personalized = real_samples >= 10

    title = (
        f"Policy Weights  ({real_samples} real samples — learned model active)"
        if personalized
        else f"Policy Weights  (heuristic warm-start — {real_samples}/10 decisions to activate)"
    )
    table = Table(title=title, show_lines=False)
    table.add_column("Feature", style="bold")
    table.add_column("Prior (warm-start)", justify="right")
    if personalized:
        table.add_column("Current (learned)", justify="right")
        table.add_column("Delta", justify="right")

    current_coef = classifier.clf.coef_[0]
    prior_coef = classifier.prior_coef

    for i, name in enumerate(FEATURE_NAMES):
        prior = prior_coef[i]
        if personalized:
            current = current_coef[i]
            delta = current - prior
            delta_str = f"{delta:+.4f}"
            color = "green" if delta > 0.01 else ("red" if delta < -0.01 else "dim")
            table.add_row(
                name,
                f"{prior:.4f}",
                f"{current:.4f}",
                f"[{color}]{delta_str}[/{color}]",
            )
        else:
            table.add_row(name, f"{prior:.4f}")

    print(table)
    if not personalized:
        print(
            f"[dim]Interact with {10 - real_samples} more check-ins to activate the personalized model.[/dim]"
        )


def clear_traces(
    yes: bool = typer.Option(False, "--yes", help="Confirm clearing decision traces."),
    file: str | None = typer.Option(None, "--file", help="Clear traces for a single file only."),
):
    """Clear decision traces, resetting policy to cold-start."""
    if not yes:
        print("[red]Refusing to clear traces without --yes.[/red]")
        raise typer.Exit(code=1)
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)
    if file:
        removed = trust_db.clear_traces_for_file(repo_root_str, file)
        if removed:
            print(f"[green]Cleared {removed} traces for {file}.[/green]")
        else:
            print(f"[yellow]No traces found for {file}.[/yellow]")
    else:
        removed = trust_db.clear_traces(repo_root_str)
        if removed:
            print(f"[green]Cleared {removed} decision traces.[/green]")
        else:
            print("[yellow]No decision traces found.[/yellow]")


def report(
    json_out: bool = typer.Option(False, "--json", help="Output report as JSON."),
):
    """Show a compact governance report for demo/readiness checks."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)
    rows = trust_db.list_traces(repo_root_str, limit=5000)
    checkins = trust_db.checkin_calibration(repo_root_str)
    checkin_quality = trust_db.checkin_usefulness_summary(repo_root_str)
    plan_summary = trust_db.plan_revision_summary(repo_root_str)
    verification_total, verification_passed = trust_db.verification_summary(repo_root_str)

    stage_counts: dict[str, int] = {}
    decision_counts: dict[str, int] = {}
    rubber_stamp_approvals = 0
    thoughtful_approvals = 0
    for row in rows:
        stage = str(row["stage"])
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        decision = str(row["user_decision"])
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        if _is_approval_decision(decision):
            if row["rubber_stamp"] == 1:
                rubber_stamp_approvals += 1
            else:
                thoughtful_approvals += 1

    model_confidence_values = [
        float(row["model_confidence_self_report"])
        for row in rows
        if row["check_in_initiator"] == "model_proactive"
        and row["model_confidence_self_report"] is not None
    ]

    payload = {
        "trace_rows": len(rows),
        "stage_counts": stage_counts,
        "decision_counts": decision_counts,
        "checkin_calibration": [
            {
                "initiator": item.initiator,
                "stage": item.stage,
                "total": item.total,
                "approval_rate": item.approval_rate,
            }
            for item in checkins
        ],
        "checkin_usefulness": [
            {
                "initiator": row.initiator,
                "total": row.total,
                "useful": row.useful,
                "wasted": row.wasted,
                "useful_rate": row.useful_rate,
            }
            for row in checkin_quality
        ],
        "model_confidence": {
            "count": len(model_confidence_values),
            "avg": (
                (sum(model_confidence_values) / len(model_confidence_values))
                if model_confidence_values
                else None
            ),
        },
        "review_quality": {
            "rubber_stamp_approvals": rubber_stamp_approvals,
            "thoughtful_approvals": thoughtful_approvals,
            "rubber_stamp_threshold_seconds": 5.0,
        },
        "plan_revisions": {
            "total": plan_summary.total,
            "approved": plan_summary.approved,
            "revisions_requested": plan_summary.revisions_requested,
            "denied": plan_summary.denied,
        },
        "verification": {
            "total": verification_total,
            "passed": verification_passed,
            "pass_rate": (verification_passed / verification_total) if verification_total else None,
        },
    }
    if json_out:
        print(json.dumps(payload, indent=2))
        return

    print("[bold]Governance report[/bold]")
    print(f"Trace rows: {payload['trace_rows']}")
    print("Stage counts:")
    for key in sorted(stage_counts):
        print(f"  - {key}: {stage_counts[key]}")
    print("Decision counts:")
    for key in sorted(decision_counts):
        print(f"  - {key}: {decision_counts[key]}")
    if checkin_quality:
        print("Check-in calibration:")
        for row in checkin_quality:
            print(
                f"  - {row.initiator}: high-signal={row.useful}/{row.total} "
                f"({row.useful_rate * 100:.1f}%), low-signal={row.wasted}"
            )
    if model_confidence_values:
        avg_conf = sum(model_confidence_values) / len(model_confidence_values)
        print(
            f"Model confidence (model-proactive check-ins): "
            f"n={len(model_confidence_values)}, avg={avg_conf:.2f}"
        )
    print(
        "Review timing: "
        f"deliberate approvals={thoughtful_approvals}, "
        f"quick approvals (<5s)={rubber_stamp_approvals}"
    )
    print(
        "Plan revisions: "
        f"total={plan_summary.total}, approved={plan_summary.approved}, "
        f"revise={plan_summary.revisions_requested}, denied={plan_summary.denied}"
    )
    if verification_total:
        rate = verification_passed / verification_total
        print(
            "Verification: "
            f"{verification_passed}/{verification_total} passed ({rate * 100:.1f}%)"
        )
    else:
        print("Verification: no recorded verification runs yet")

    if checkins:
        table = Table(title="Check-in Calibration Snapshot")
        table.add_column("Initiator")
        table.add_column("Stage")
        table.add_column("Total")
        table.add_column("Approval %")
        for row in checkins:
            table.add_row(
                row.initiator,
                row.stage,
                str(row.total),
                f"{row.approval_rate * 100:.1f}",
            )
        print(table)

    sample_count = trust_db.policy_model_sample_count(repo_root_str)
    if sample_count >= 10:
        print(f"\n[green]Learned policy active — {sample_count} real decisions incorporated.[/green]")
        print("[dim]Run `hw observe weights` to inspect coefficient drift from heuristic priors.[/dim]")
    else:
        print(f"\n[dim]Policy: heuristic warm-start ({sample_count}/10 decisions to activate learned model).[/dim]")


def _session_summary(rows: list[dict]) -> dict[str, object]:
    if not rows:
        return {
            "trace_rows": 0,
            "stage_counts": {},
            "decision_counts": {},
        }
    stage_counts: dict[str, int] = {}
    decision_counts: dict[str, int] = {}
    for row in rows:
        stage = str(row["stage"])
        decision = str(row["user_decision"])
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
    first = rows[0]
    return {
        "session_id": first["session_id"],
        "participant_id": first["participant_id"],
        "study_run_id": first["study_run_id"],
        "study_task_id": first["study_task_id"],
        "autonomy_mode": first["autonomy_mode"],
        "trace_rows": len(rows),
        "stage_counts": stage_counts,
        "decision_counts": decision_counts,
    }


def export(
    out: Path = typer.Option(
        Path(".sc/exports"),
        "--out",
        help="Directory to write session export artifacts into.",
    ),
    session_id: str | None = typer.Option(
        None,
        "--session-id",
        help="Session id to export. Defaults to the latest recorded session.",
    ),
):
    """Export the latest session bundle for lab analysis."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)
    resolved_session_id = session_id or trust_db.latest_session_id(repo_root_str)
    if not resolved_session_id:
        print("[yellow]No recorded sessions to export.[/yellow]")
        raise typer.Exit(code=1)

    rows = [dict(row) for row in trust_db.session_traces(repo_root_str, resolved_session_id)]
    revisions = [dict(row) for row in trust_db.session_plan_revisions(repo_root_str, resolved_session_id)]
    if not rows:
        print(f"[yellow]No traces found for session {resolved_session_id}.[/yellow]")
        raise typer.Exit(code=1)

    output_dir = out if out.is_absolute() else (repo_root / out)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = resolved_session_id
    traces_csv_path = output_dir / f"{stem}_traces.csv"
    bundle_json_path = output_dir / f"{stem}_bundle.json"

    fieldnames = list(rows[0].keys())
    with traces_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    config = load_config(repo_root)
    prefs = trust_db.autonomy_preferences(repo_root_str)
    bundle = {
        "repo_root": repo_root_str,
        "summary": _session_summary(rows),
        "config": config.to_dict() if config else None,
        "constraints": [item.__dict__ for item in trust_db.list_constraints(repo_root_str)],
        "guidelines": [item.__dict__ for item in trust_db.list_behavioral_guidelines(repo_root_str)],
        "preferences": {
            "prefer_fewer_checkins": prefs.prefer_fewer_checkins,
            "allowed_checkin_topics": list(prefs.allowed_checkin_topics),
            "skip_low_risk_plan_checkpoint": prefs.skip_low_risk_plan_checkpoint,
            "scoped_paths": list(prefs.scoped_paths),
        },
        "plan_revisions": revisions,
        "traces": rows,
    }
    bundle_json_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    print("[green]Export complete.[/green]")
    print(f"  Session: {resolved_session_id}")
    print(f"  Bundle: {bundle_json_path}")
    print(f"  CSV: {traces_csv_path}")


def reset(
    yes: bool = typer.Option(False, "--yes", help="Confirm resetting all learned state."),
):
    """Reset all learned state (history, access grants, preferences, and ML model) to cold-start."""
    if not yes:
        print("[red]Refusing to reset without --yes.[/red]")
        raise typer.Exit(code=1)
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)
    cleared_traces = trust_db.clear_traces(repo_root_str)
    cleared_revisions = trust_db.clear_plan_revisions(repo_root_str)
    revoked_leases, revoked_decisions = trust_db.revoke(repo_root_str, file_path=None, reset_counts=True)
    cleared_prefs = trust_db.delete_autonomy_preferences(repo_root_str)
    trust_db.delete_policy_model(repo_root_str)
    from ..ml_policy import build_warm_start_classifier
    classifier = build_warm_start_classifier()
    trust_db.save_policy_model(repo_root_str, classifier)
    print(f"[green]Reset complete:[/green]")
    print(
        f"  History: cleared {cleared_traces} traces, "
        f"{cleared_revisions} plan revisions, {revoked_decisions} approval records"
    )
    print(f"  Access: revoked {revoked_leases} leases")
    print(f"  Preferences: {'cleared' if cleared_prefs else 'none to clear'}")
    print(f"  Policy model: reset to heuristic warm-start (0 real samples)")

def revoke(
    path: str | None = typer.Argument(None, help="Repo-relative file path whose lease to revoke."),
    all: bool = typer.Option(False, "--all", help="Revoke all file access leases for this repo."),
):
    """Revoke file access leases for a path (or all). To revoke learned preferences, use preferences-revoke."""
    if not path and not all:
        print("[red]Provide a path or --all.[/red]")
        raise typer.Exit(code=1)

    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    normalized = None
    if path:
        normalized = str(Path(path))
    removed_leases, removed_decisions = trust_db.revoke(
        str(repo_root),
        file_path=normalized if not all else None,
        reset_counts=True,
    )
    print(f"[green]Revoked {removed_leases} leases.[/green]")
    if removed_decisions:
        print(f"[green]Cleared {removed_decisions} approval records.[/green]")
