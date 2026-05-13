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


def preferences(
    json_out: bool = typer.Option(False, "--json", help="Output learned autonomy preferences as JSON."),
    verbose: bool = typer.Option(False, "--verbose", help="Show the full preference table + scoring bands."),
):
    """What has Hedwig learned about how you want oversight to work?

    Default: a plain-English summary.
    With --verbose: the preference table + effective scoring bands.
    """
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

    from ..run.theme import PALETTE, panel_title

    # ---------- Default path: short prose ----------
    if not verbose:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text

        console = Console()
        body = Text()

        any_pref = (
            prefs.prefer_fewer_checkins
            or prefs.skip_low_risk_plan_checkpoint
            or prefs.allowed_checkin_topics
            or prefs.scoped_paths
        )

        if not any_pref:
            body.append(
                "Nothing learned yet — Hedwig is running with defaults.\n\n",
                style="white",
            )
            body.append(
                "As you approve, deny, and correct across sessions, I'll "
                "surface hypotheses for you to confirm or decline.",
                style=PALETTE["meta_italic"],
            )
            console.print(
                Panel(
                    body,
                    title=panel_title("info", "what I've learned"),
                    border_style=PALETTE["info"],
                    padding=(1, 2),
                )
            )
            return

        body.append("Here's what I'm operating on right now:\n\n", style="white")
        if prefs.prefer_fewer_checkins:
            body.append(
                "  · You prefer fewer check-ins on low-risk work.\n", style="white"
            )
        if prefs.skip_low_risk_plan_checkpoint:
            body.append(
                "  · I skip the plan checkpoint on low-risk multi-file cleanups.\n",
                style="white",
            )
        if prefs.allowed_checkin_topics:
            topics = ", ".join(prefs.allowed_checkin_topics)
            body.append(
                f"  · I check in specifically for: {topics}.\n",
                style="white",
            )
        if prefs.scoped_paths:
            scopes = ", ".join(prefs.scoped_paths)
            body.append(
                f"  · Preferences scoped to: {scopes}.\n",
                style="white",
            )
        body.append("\n")

        # Effective thresholds, in plain language.
        body.append("Effective oversight thresholds:\n", style=PALETTE["info_bold"])
        body.append(
            f"  · above {adj_proceed:.2f} — auto-approve silently\n",
            style=PALETTE["approve"],
        )
        body.append(
            f"  · {adj_flag:.2f} to {adj_proceed:.2f} — auto-approve, flag in summary\n",
            style=PALETTE["attention"],
        )
        body.append(
            f"  · below {adj_flag:.2f} — check in with you\n",
            style=PALETTE["deny"],
        )

        console.print(
            Panel(
                body,
                title=panel_title("info", "what I've learned"),
                border_style=PALETTE["info"],
                padding=(1, 2),
            )
        )
        print(
            f"[{PALETTE['meta']}]· revoke:[/{PALETTE['meta']}] "
            "hw observe preferences-revoke --fewer-checkins | --topic <name> | --path <path>"
        )
        return

    # ---------- Verbose path: original table view ----------
    def _yesno(flag: bool) -> str:
        if flag:
            return f"[{PALETTE['approve_bold']}]yes[/{PALETTE['approve_bold']}]"
        return f"[{PALETTE['meta']}]no[/{PALETTE['meta']}]"

    def _values_or_dash(values: tuple[str, ...], color_key: str) -> str:
        if not values:
            return f"[{PALETTE['meta']}]—[/{PALETTE['meta']}]"
        return ", ".join(
            f"[{PALETTE[color_key]}]{v}[/{PALETTE[color_key]}]" for v in values
        )

    table = Table(
        title=panel_title("observe", "autonomy preferences"),
        title_justify="left",
        show_lines=False,
        padding=(0, 1),
        border_style=PALETTE["info_dim"],
        header_style=PALETTE["info_bold"],
    )
    table.add_column("Preference", no_wrap=True)
    table.add_column("Value")
    table.add_row("prefer fewer check-ins", _yesno(prefs.prefer_fewer_checkins))
    table.add_row(
        "allowed check-in topics",
        _values_or_dash(prefs.allowed_checkin_topics, "info"),
    )
    table.add_row(
        "skip low-risk plan checkpoints",
        _yesno(prefs.skip_low_risk_plan_checkpoint),
    )
    table.add_row(
        "scoped paths",
        _values_or_dash(prefs.scoped_paths, "info"),
    )
    print(table)
    print()

    # Effective scoring bands — shows how preferences shifted the thresholds.
    threshold_table = Table(
        title=panel_title("observe", "effective scoring bands"),
        title_justify="left",
        show_lines=False,
        padding=(0, 1),
        border_style=PALETTE["info_dim"],
        header_style=PALETTE["info_bold"],
    )
    threshold_table.add_column("Score", no_wrap=True, width=22)
    threshold_table.add_column("Action")
    threshold_table.add_row(
        f"[{PALETTE['approve']}]≥ {adj_proceed:.2f}[/{PALETTE['approve']}]",
        f"[{PALETTE['approve']}]auto-approve · silent[/{PALETTE['approve']}]",
    )
    threshold_table.add_row(
        f"[{PALETTE['attention']}]≥ {adj_flag:.2f} and < {adj_proceed:.2f}[/{PALETTE['attention']}]",
        f"[{PALETTE['attention']}]auto-approve · flagged for summary[/{PALETTE['attention']}]",
    )
    threshold_table.add_row(
        f"[{PALETTE['deny']}]< {adj_flag:.2f}[/{PALETTE['deny']}]",
        f"[{PALETTE['deny']}]check-in required[/{PALETTE['deny']}]",
    )
    print(threshold_table)
    print(
        f"[{PALETTE['meta']}]· revoke a preference:[/{PALETTE['meta']}] "
        "hw observe preferences-revoke --fewer-checkins | --topic <name> | --path <path>"
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


def weights(
    verbose: bool = typer.Option(False, "--verbose", help="Show per-feature coefficient drift table."),
):
    """What has Hedwig picked up from your interactions?

    Default: a plain summary of what's moved.
    With --verbose: per-feature coefficient drift with visual bars.
    For the full picture, use `hw observe export --html`.
    """
    from ..ml_policy import FEATURE_NAMES
    from ..run.theme import PALETTE, panel_title

    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    classifier = trust_db.load_policy_model(str(repo_root))

    if classifier is None:
        print(
            f"[{PALETTE['attention']}]Hedwig isn't set up for this repo yet. "
            f"Run[/{PALETTE['attention']}] hw init [{PALETTE['attention']}]first.[/{PALETTE['attention']}]"
        )
        raise typer.Exit(code=0)

    real_samples = classifier.sample_count
    personalized = real_samples >= 10

    # ---------- Default path: short prose ----------
    if not verbose:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text

        console = Console()
        body = Text()

        if not personalized:
            body.append(
                f"Hedwig is still in cold-start mode — "
                f"{real_samples}/10 real decisions needed before the learned "
                f"scorer activates.\n\n",
                style="white",
            )
            body.append(
                "Until then, Hedwig uses the heuristic scorer.",
                style=PALETTE["meta_italic"],
            )
            console.print(
                Panel(
                    body,
                    title=panel_title("info", "what I've picked up"),
                    border_style=PALETTE["info"],
                    padding=(1, 2),
                )
            )
            return

        # Compute top movers in each direction.
        current = classifier.clf.coef_[0]
        prior = classifier.prior_coef
        movers = [(FEATURE_NAMES[i], current[i] - prior[i]) for i in range(len(FEATURE_NAMES))]
        strengthened = sorted([(n, d) for n, d in movers if d > 0.01], key=lambda x: -x[1])[:3]
        weakened = sorted([(n, d) for n, d in movers if d < -0.01], key=lambda x: x[1])[:3]

        # Plain-English name map so "prior_approvals" reads as "prior approvals".
        def _humanize(feature: str) -> str:
            return feature.replace("_", " ")

        body.append(
            f"Trained on {real_samples} of your real decisions. "
            f"Here's what's moved most:\n\n",
            style="white",
        )
        if strengthened:
            body.append(
                "Signals that now push toward auto-approving:\n",
                style=PALETTE["approve_bold"],
            )
            for name, delta in strengthened:
                body.append(f"  · {_humanize(name)} ", style="white")
                body.append(f"(+{delta:.2f})\n", style=PALETTE["approve"])
            body.append("\n")
        if weakened:
            body.append(
                "Signals that now push toward checking in:\n",
                style=PALETTE["deny_bold"],
            )
            for name, delta in weakened:
                body.append(f"  · {_humanize(name)} ", style="white")
                body.append(f"({delta:.2f})\n", style=PALETTE["deny"])
            body.append("\n")
        if not strengthened and not weakened:
            body.append(
                "Coefficients haven't moved much yet. Keep going — patterns "
                "emerge after ~20 decisions.",
                style=PALETTE["meta_italic"],
            )

        console.print(
            Panel(
                body,
                title=panel_title("info", "what I've picked up"),
                border_style=PALETTE["info"],
                padding=(1, 2),
            )
        )
        print(
            f"[{PALETTE['meta']}]Use[/{PALETTE['meta']}] hw observe weights --verbose "
            f"[{PALETTE['meta']}]for the full feature table, or[/{PALETTE['meta']}] "
            f"hw observe export --html "
            f"[{PALETTE['meta']}]for a browser report.[/{PALETTE['meta']}]"
        )
        return

    # ---------- Verbose path: original detailed table ----------
    title_suffix = (
        f"{real_samples} decisions · learned model active"
        if personalized
        else f"cold-start · {real_samples}/10 decisions to activate"
    )
    table = Table(
        title=panel_title("observe", f"learned weights · {title_suffix}"),
        title_justify="left",
        show_lines=False,
        padding=(0, 1),
        border_style=PALETTE["info_dim"],
        header_style=PALETTE["info_bold"],
    )
    table.add_column("Feature", style="bold", no_wrap=True)
    table.add_column("Prior", justify="right", no_wrap=True)
    if personalized:
        table.add_column("Current", justify="right", no_wrap=True)
        table.add_column("Delta", justify="right", no_wrap=True)
        # Visual bar column — width scaled to largest absolute delta.
        table.add_column("Drift", no_wrap=True, width=20)

    current_coef = classifier.clf.coef_[0]
    prior_coef = classifier.prior_coef

    max_abs_delta = 0.0
    if personalized:
        max_abs_delta = max(
            (abs(current_coef[i] - prior_coef[i]) for i in range(len(FEATURE_NAMES))),
            default=0.0,
        )

    for i, name in enumerate(FEATURE_NAMES):
        prior = prior_coef[i]
        if personalized:
            current = current_coef[i]
            delta = current - prior
            delta_str = f"{delta:+.3f}"
            if delta > 0.01:
                color = PALETTE["approve"]
            elif delta < -0.01:
                color = PALETTE["deny"]
            else:
                color = PALETTE["meta"]

            # Build a visual bar — up to 16 chars total, centered at zero.
            if max_abs_delta > 0:
                bar_len = int(round(abs(delta) / max_abs_delta * 8))
            else:
                bar_len = 0
            bar_char = "█"
            if delta > 0:
                bar = (
                    f"[{PALETTE['meta']}]{' ' * 8}[/{PALETTE['meta']}]"
                    f"[{PALETTE['approve']}]{bar_char * bar_len}[/{PALETTE['approve']}]"
                )
            elif delta < 0:
                bar = (
                    f"[{PALETTE['meta']}]{' ' * (8 - bar_len)}[/{PALETTE['meta']}]"
                    f"[{PALETTE['deny']}]{bar_char * bar_len}[/{PALETTE['deny']}]"
                )
            else:
                bar = f"[{PALETTE['meta']}]{' ' * 8}·[/{PALETTE['meta']}]"

            table.add_row(
                name,
                f"{prior:+.3f}",
                f"{current:+.3f}",
                f"[{color}]{delta_str}[/{color}]",
                bar,
            )
        else:
            table.add_row(name, f"{prior:+.3f}")

    print(table)
    if not personalized:
        print(
            f"[{PALETTE['meta']}]· {10 - real_samples} more decisions to activate the personalized model[/{PALETTE['meta']}]"
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
    verbose: bool = typer.Option(False, "--verbose", help="Show the full data tables."),
):
    """Summarize Hedwig activity in this repo.

    Default: a short paragraph on what's happened.
    With --verbose: per-stage/decision tables, calibration, verification details.
    For researcher-depth analysis, use `hw observe export --html`.
    """
    from ..regret import regret_summary

    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)
    rows = trust_db.list_traces(repo_root_str, limit=5000)
    # list_traces returns DESC; regret detection needs chronological order.
    regret = regret_summary([dict(r) for r in reversed(rows)])
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
        "regret": regret,
    }
    if json_out:
        print(json.dumps(payload, indent=2))
        return

    from ..run.theme import PALETTE, panel_title

    # ---------- Default path: short prose, no jargon ----------
    if not verbose:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text

        console = Console()
        body = Text()

        if not rows:
            body.append(
                "We haven't recorded any activity in this repo yet.\n\n",
                style="white",
            )
            body.append("Run ", style=PALETTE["meta"])
            body.append("hw '<task>'", style=PALETTE["info_bold"])
            body.append(" to get started.", style=PALETTE["meta"])
            console.print(
                Panel(
                    body,
                    title=panel_title("info", "activity"),
                    border_style=PALETTE["info"],
                    padding=(1, 2),
                )
            )
            return

        # High-level count sentence.
        total_actions = payload["trace_rows"]
        approves = sum(
            v for k, v in decision_counts.items()
            if k.startswith("approve") or k.startswith("auto")
        )
        denies = decision_counts.get("deny", 0)
        body.append(
            f"Across {total_actions} actions in this repo so far, "
            f"you've approved {approves}",
            style="white",
        )
        if denies:
            body.append(f" and denied {denies}", style="white")
        body.append(".\n\n", style="white")

        # Check-in calibration, in prose.
        model_qa = next((q for q in checkin_quality if q.initiator == "model_proactive"), None)
        policy_qa = next((q for q in checkin_quality if q.initiator == "policy"), None)
        if model_qa or policy_qa:
            body.append("Check-ins so far:\n", style=PALETTE["info_bold"])
            if model_qa and model_qa.total:
                pct = model_qa.useful_rate * 100
                body.append(
                    f"  · the model asked {model_qa.total} times — "
                    f"{pct:.0f}% landed on a real decision.\n",
                    style="white",
                )
            if policy_qa and policy_qa.total:
                pct = policy_qa.useful_rate * 100
                body.append(
                    f"  · Hedwig paused you {policy_qa.total} times — "
                    f"{pct:.0f}% were high-signal moments.\n",
                    style="white",
                )
            body.append("\n")

        # Review quality, in prose.
        total_approvals = thoughtful_approvals + rubber_stamp_approvals
        if total_approvals:
            if thoughtful_approvals >= rubber_stamp_approvals:
                body.append(
                    f"You reviewed carefully on most approvals "
                    f"({thoughtful_approvals} deliberate, "
                    f"{rubber_stamp_approvals} quick).\n",
                    style="white",
                )
            else:
                body.append(
                    f"Most approvals were quick "
                    f"({rubber_stamp_approvals} quick, "
                    f"{thoughtful_approvals} deliberate).\n",
                    style=PALETTE["attention"],
                )

        # Verification, in prose.
        if verification_total:
            rate = verification_passed / verification_total
            if rate >= 0.8:
                body.append(
                    f"Verification runs passed {verification_passed}/{verification_total} "
                    f"({rate * 100:.0f}%).\n",
                    style=PALETTE["approve"],
                )
            else:
                body.append(
                    f"Verification passed {verification_passed}/{verification_total} "
                    f"({rate * 100:.0f}%) — worth looking at the failures.\n",
                    style=PALETTE["attention"],
                )

        # Regret — autonomy honesty check.
        if regret["total"]:
            body.append(
                f"Hedwig auto-approved {regret['total']} action"
                f"{'s' if regret['total'] != 1 else ''} you later pushed back on.\n",
                style=PALETTE["attention"],
            )

        # Learning state, in prose.
        sample_count = trust_db.policy_model_sample_count(repo_root_str)
        body.append("\n")
        if sample_count >= 10:
            body.append(
                f"Hedwig's learned scorer is active, trained on "
                f"{sample_count} of your real decisions.",
                style=PALETTE["learn_bold"],
            )
        else:
            body.append(
                f"Hedwig is still in cold-start mode "
                f"({sample_count}/10 decisions to activate the learned scorer).",
                style=PALETTE["meta"],
            )

        console.print(
            Panel(
                body,
                title=panel_title("info", "activity"),
                border_style=PALETTE["info"],
                padding=(1, 2),
            )
        )
        print(
            f"[{PALETTE['meta']}]Use[/{PALETTE['meta']}] hw observe report --verbose "
            f"[{PALETTE['meta']}]for tables, or[/{PALETTE['meta']}] "
            f"hw observe export --html "
            f"[{PALETTE['meta']}]for a browser report.[/{PALETTE['meta']}]"
        )
        return

    # ---------- Verbose path: original detailed output ----------
    print(panel_title("observe", "governance report"))
    print(
        f"[{PALETTE['meta']}]trace rows:[/{PALETTE['meta']}] "
        f"[{PALETTE['info_bold']}]{payload['trace_rows']}[/{PALETTE['info_bold']}]"
    )
    print()

    if stage_counts:
        stage_table = Table(
            title=panel_title("observe", "stage · decision distribution"),
            title_justify="left",
            show_lines=False,
            padding=(0, 1),
            border_style=PALETTE["info_dim"],
            header_style=PALETTE["info_bold"],
        )
        stage_table.add_column("Stage", no_wrap=True)
        stage_table.add_column("Count", justify="right", no_wrap=True)
        for key in sorted(stage_counts):
            stage_table.add_row(key, f"[{PALETTE['info']}]{stage_counts[key]}[/{PALETTE['info']}]")
        print(stage_table)

    if decision_counts:
        decision_table = Table(
            title=panel_title("observe", "decisions"),
            title_justify="left",
            show_lines=False,
            padding=(0, 1),
            border_style=PALETTE["info_dim"],
            header_style=PALETTE["info_bold"],
        )
        decision_table.add_column("Decision", no_wrap=True)
        decision_table.add_column("Count", justify="right", no_wrap=True)
        for key in sorted(decision_counts):
            color = PALETTE["approve"] if key.startswith("approve") or key.startswith("auto") else (
                PALETTE["deny"] if key == "deny" else PALETTE["info"]
            )
            decision_table.add_row(key, f"[{color}]{decision_counts[key]}[/{color}]")
        print(decision_table)

    if checkin_quality:
        print()
        print(f"[{PALETTE['info_bold']}]check-in calibration[/{PALETTE['info_bold']}]")
        for row in checkin_quality:
            useful_pct = row.useful_rate * 100
            pct_color = PALETTE["approve"] if useful_pct >= 70 else (
                PALETTE["attention"] if useful_pct >= 40 else PALETTE["deny"]
            )
            print(
                f"  [{PALETTE['meta']}]·[/{PALETTE['meta']}] "
                f"{row.initiator}: high-signal "
                f"[{pct_color}]{row.useful}/{row.total}[/{pct_color}] "
                f"([{pct_color}]{useful_pct:.1f}%[/{pct_color}]), "
                f"low-signal [{PALETTE['meta']}]{row.wasted}[/{PALETTE['meta']}]"
            )

    if model_confidence_values:
        avg_conf = sum(model_confidence_values) / len(model_confidence_values)
        print(
            f"[{PALETTE['meta']}]model confidence · model-proactive check-ins:[/{PALETTE['meta']}] "
            f"n={len(model_confidence_values)}, avg={avg_conf:.2f}"
        )

    # Review timing
    deliberate_color = PALETTE["approve"] if thoughtful_approvals > rubber_stamp_approvals else PALETTE["attention"]
    print()
    print(
        f"[{PALETTE['meta']}]review timing:[/{PALETTE['meta']}] "
        f"[{deliberate_color}]{thoughtful_approvals} deliberate[/{deliberate_color}] · "
        f"[{PALETTE['meta']}]{rubber_stamp_approvals} rubber-stamp (<5s)[/{PALETTE['meta']}]"
    )
    print(
        f"[{PALETTE['meta']}]plan revisions:[/{PALETTE['meta']}] "
        f"{plan_summary.total} total · "
        f"[{PALETTE['approve']}]{plan_summary.approved} approved[/{PALETTE['approve']}] · "
        f"[{PALETTE['attention']}]{plan_summary.revisions_requested} revised[/{PALETTE['attention']}] · "
        f"[{PALETTE['deny']}]{plan_summary.denied} denied[/{PALETTE['deny']}]"
    )
    if verification_total:
        rate = verification_passed / verification_total
        rate_color = PALETTE["approve"] if rate >= 0.8 else (
            PALETTE["attention"] if rate >= 0.5 else PALETTE["deny"]
        )
        print(
            f"[{PALETTE['meta']}]verification:[/{PALETTE['meta']}] "
            f"[{rate_color}]{verification_passed}/{verification_total} passed "
            f"({rate * 100:.1f}%)[/{rate_color}]"
        )
    else:
        print(f"[{PALETTE['meta']}]verification:[/{PALETTE['meta']}] no recorded runs yet")

    if checkins:
        print()
        cal_table = Table(
            title=panel_title("observe", "check-in calibration snapshot"),
            title_justify="left",
            show_lines=False,
            padding=(0, 1),
            border_style=PALETTE["info_dim"],
            header_style=PALETTE["info_bold"],
        )
        cal_table.add_column("Initiator", no_wrap=True)
        cal_table.add_column("Stage", no_wrap=True)
        cal_table.add_column("Total", justify="right", no_wrap=True)
        cal_table.add_column("Approval %", justify="right", no_wrap=True)
        for row in checkins:
            pct = row.approval_rate * 100
            pct_color = PALETTE["approve"] if pct >= 70 else (
                PALETTE["attention"] if pct >= 40 else PALETTE["deny"]
            )
            cal_table.add_row(
                row.initiator,
                row.stage,
                str(row.total),
                f"[{pct_color}]{pct:.1f}%[/{pct_color}]",
            )
        print(cal_table)

    sample_count = trust_db.policy_model_sample_count(repo_root_str)
    print()
    if sample_count >= 10:
        print(
            f"[{PALETTE['learn_bold']}]✦ learned policy active[/{PALETTE['learn_bold']}] "
            f"— {sample_count} real decisions incorporated"
        )
        print(
            f"[{PALETTE['meta']}]  run[/{PALETTE['meta']}] hw observe weights "
            f"[{PALETTE['meta']}]to inspect coefficient drift[/{PALETTE['meta']}]"
        )
    else:
        print(
            f"[{PALETTE['meta']}]· cold-start scorer active "
            f"({sample_count}/10 decisions to activate learned model)[/{PALETTE['meta']}]"
        )


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
        help="Directory to write export artifacts into.",
    ),
    session_id: str | None = typer.Option(
        None,
        "--session-id",
        help="Session id to export. Defaults to the latest recorded session.",
    ),
    html_report: bool = typer.Option(
        False,
        "--html",
        help="Generate a single-file HTML report (researcher view) and open it.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="When combined with --html, open the report in your default browser.",
    ),
):
    """Export Hedwig's learned state for review.

    Default: writes a per-session CSV + JSON bundle to .sc/exports/.
    Use --html to write a single-file browser-friendly report instead.
    """
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)

    # HTML report path — single-file, repo-wide (not per-session).
    if html_report:
        from ..export_report import write_report
        from ..run.theme import PALETTE
        import webbrowser

        output_dir = out if out.is_absolute() else (repo_root / out)
        report_path = output_dir / "hedwig_report.html"
        write_report(trust_db, repo_root_str, report_path)
        print(
            f"[{PALETTE['approve_bold']}]✓ report generated[/{PALETTE['approve_bold']}]"
        )
        print(
            f"  [{PALETTE['meta']}]file:[/{PALETTE['meta']}] "
            f"[{PALETTE['info']}]{report_path}[/{PALETTE['info']}]"
        )
        if open_browser:
            try:
                webbrowser.open(report_path.as_uri())
                print(f"  [{PALETTE['meta']}]opened in your default browser[/{PALETTE['meta']}]")
            except Exception:
                print(
                    f"  [{PALETTE['attention']}]could not open browser; open the file manually[/{PALETTE['attention']}]"
                )
        return

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
    from ..ml_policy import build_cold_classifier
    classifier = build_cold_classifier()
    trust_db.save_policy_model(repo_root_str, classifier)
    print("[green]Reset complete:[/green]")
    print(
        f"  History: cleared {cleared_traces} traces, "
        f"{cleared_revisions} plan revisions, {revoked_decisions} approval records"
    )
    print(f"  Access: revoked {revoked_leases} leases")
    print(f"  Preferences: {'cleared' if cleared_prefs else 'none to clear'}")
    print("  Policy model: reset to heuristic warm-start (0 real samples)")

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


def rollback(
    snapshot_id: int | None = typer.Argument(
        None, help="Snapshot id to restore (default: most recent)."
    ),
    list_only: bool = typer.Option(
        False, "--list", help="List available snapshots instead of restoring."
    ),
) -> None:
    """Roll back the learned classifier to a prior snapshot.

    Hedwig snapshots the classifier before every save. If a recent update
    made the scorer worse, `hw observe rollback` restores the last known
    state. With no argument, the most recent snapshot is restored and
    consumed.
    """
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)
    snapshots = trust_db.list_policy_model_snapshots(repo_root_str)

    if list_only:
        if not snapshots:
            print("[dim]No snapshots available yet.[/dim]")
            return
        table = Table(title="Policy model snapshots")
        table.add_column("id", justify="right")
        table.add_column("samples", justify="right")
        table.add_column("saved")
        for s in snapshots:
            table.add_row(
                str(s["id"]),
                str(s["sample_count"]),
                _format_timestamp(s["created_at"]),
            )
        print(table)
        return

    if not snapshots:
        print("[yellow]No snapshots to roll back to.[/yellow]")
        raise typer.Exit(code=1)

    ok = trust_db.restore_policy_model_snapshot(repo_root_str, snapshot_id)
    if not ok:
        print(f"[red]Snapshot {snapshot_id} not found.[/red]")
        raise typer.Exit(code=1)

    remaining = len(snapshots) - 1
    target = snapshot_id if snapshot_id is not None else snapshots[0]["id"]
    print(f"[green]Restored snapshot {target}.[/green]")
    print(f"[dim]{remaining} snapshot{'s' if remaining != 1 else ''} remaining.[/dim]")


def personas(
    limit: int = typer.Option(5, "--limit", help="Max sessions to summarize."),
    verbose: bool = typer.Option(False, "--verbose", help="Show the full per-session table."),
) -> None:
    """Summarize recent sessions in plain English.

    Default: one sentence per session.
    With --verbose: the full per-session table with signal breakdowns.
    """
    from collections import Counter

    from ..preference_inference import (
        infer_user_persona,
        summarize_session,
    )
    from ..run.theme import PALETTE, panel_title

    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)

    with trust_db._connect() as conn:
        session_ids = [
            row["session_id"]
            for row in conn.execute(
                """
                SELECT session_id, MAX(created_at) AS latest
                FROM decision_traces
                WHERE repo_root = ?
                GROUP BY session_id
                ORDER BY latest DESC
                LIMIT ?
                """,
                (repo_root_str, limit),
            ).fetchall()
        ]

    if not session_ids:
        print(
            f"[{PALETTE['meta']}]No sessions yet in this repo. "
            f"Run[/{PALETTE['meta']}] hw '<task>' "
            f"[{PALETTE['meta']}]to start.[/{PALETTE['meta']}]"
        )
        return

    # ---- Default path: one sentence per session, no jargon ------------
    if not verbose:
        print(panel_title("info", f"last {len(session_ids)} session{'s' if len(session_ids) != 1 else ''}"))
        print()
        for sid in session_ids:
            session_rows = trust_db.session_traces(repo_root_str, sid)
            row_dicts = [dict(r) for r in session_rows]
            summary = summarize_session(row_dicts)
            persona = infer_user_persona(summary).value

            if persona == "active":
                style = "engaged back-and-forth"
            elif persona == "delegating":
                style = "mostly delegating"
            else:
                style = "just starting"

            # Build the tail phrase honestly — only mention signals that exist.
            tail_parts: list[str] = []
            if summary.n_approvals:
                tail_parts.append(f"{summary.n_approvals} approvals")
            if summary.n_feedback:
                tail_parts.append(
                    f"{summary.n_feedback} correction{'s' if summary.n_feedback != 1 else ''}"
                )
            if summary.n_denials:
                tail_parts.append(f"{summary.n_denials} denials")
            if summary.n_failures:
                tail_parts.append(
                    f"{summary.n_failures} failure report{'s' if summary.n_failures != 1 else ''}"
                )
            tail = ", ".join(tail_parts) if tail_parts else "no developer signals yet"

            turn_word = "turn" if summary.n_turns == 1 else "turns"
            print(
                f"  [{PALETTE['meta']}]·[/{PALETTE['meta']}] "
                f"[{PALETTE['info_bold']}]{sid[:8]}[/{PALETTE['info_bold']}]  "
                f"{summary.n_turns} {turn_word}, {style} — {tail}"
            )
        print()
        print(
            f"[{PALETTE['meta']}]Run[/{PALETTE['meta']}] "
            f"hw observe personas --verbose "
            f"[{PALETTE['meta']}]for the full table, or[/{PALETTE['meta']}] "
            f"hw observe export --html "
            f"[{PALETTE['meta']}]for a browser report.[/{PALETTE['meta']}]"
        )
        return

    # ---- --verbose path: the original researcher table ----------------
    from ..preference_inference import infer_coding_mode
    from ..preferences import FAILURE_SIGNAL_CHECKIN

    def _intensity_badge(value: str) -> str:
        if value == "active":
            return f"[{PALETTE['learn_bold']}]● active[/{PALETTE['learn_bold']}]"
        if value == "delegating":
            return f"[{PALETTE['info_bold']}]○ delegating[/{PALETTE['info_bold']}]"
        return f"[{PALETTE['meta']}]· unknown[/{PALETTE['meta']}]"

    def _mode_badge(value: str) -> str:
        if value == "vibe":
            return f"[{PALETTE['learn']}]vibe[/{PALETTE['learn']}]"
        if value == "human_only":
            return f"[{PALETTE['approve']}]human[/{PALETTE['approve']}]"
        return f"[{PALETTE['info']}]collab[/{PALETTE['info']}]"

    def _mix(counter: Counter[str]) -> str:
        parts: list[str] = []
        for key, color_key in [
            ("correction", "info"),
            ("rejection", "deny"),
            ("failure_report", "deny_bold"),
            ("positive_redirect", "learn"),
            ("scope_constraint", "attention"),
            ("non_pushback", "meta"),
        ]:
            n = counter.get(key, 0)
            if n == 0:
                parts.append(f"[{PALETTE['meta']}]0[/{PALETTE['meta']}]")
            else:
                parts.append(f"[{PALETTE[color_key]}]{n}[/{PALETTE[color_key]}]")
        return f"[{PALETTE['meta']}]/[/{PALETTE['meta']}]".join(parts)

    table = Table(
        title=panel_title("observe", "sessions (verbose)"),
        title_justify="left",
        show_lines=True,
        padding=(0, 1),
        border_style=PALETTE["info_dim"],
        header_style=PALETTE["info_bold"],
        expand=False,
    )
    table.add_column("Session", no_wrap=True, width=10)
    table.add_column("Turns", justify="right", no_wrap=True, width=6)
    table.add_column("Mode", no_wrap=True, width=8)
    table.add_column("Style", no_wrap=True, width=14)
    table.add_column("Pushback mix", no_wrap=True, width=34)
    table.add_column("Approval", justify="right", no_wrap=True, width=10)
    table.add_column("Fail", justify="center", no_wrap=True, width=5)

    for sid in session_ids:
        rows = trust_db.session_traces(repo_root_str, sid)
        row_dicts = [dict(r) for r in rows]
        with trust_db._connect() as conn:
            extra = conn.execute(
                "SELECT pushback_type FROM decision_traces "
                "WHERE repo_root = ? AND session_id = ? ORDER BY created_at ASC, id ASC",
                (repo_root_str, sid),
            ).fetchall()
        summary = summarize_session(row_dicts)
        mode = infer_coding_mode(summary).value
        intensity = infer_user_persona(summary).value
        pushback_counter: Counter[str] = Counter(
            (r["pushback_type"] or "unknown") for r in extra
        )
        approval_pct = f"{summary.approval_rate * 100:.0f}%" if summary.n_turns else "—"
        min_failures_needed = FAILURE_SIGNAL_CHECKIN.condition.min_prior_failure_count or 0
        fail_match = summary.n_failures >= min_failures_needed and min_failures_needed > 0
        fail_cell = (
            f"[{PALETTE['deny_bold']}]✓[/{PALETTE['deny_bold']}]"
            if fail_match
            else f"[{PALETTE['meta']}]·[/{PALETTE['meta']}]"
        )
        table.add_row(
            f"[{PALETTE['meta']}]{sid[:8]}[/{PALETTE['meta']}]",
            str(summary.n_turns),
            _mode_badge(mode),
            _intensity_badge(intensity),
            _mix(pushback_counter),
            f"[{PALETTE['approve']}]{approval_pct}[/{PALETTE['approve']}]" if summary.n_turns else approval_pct,
            fail_cell,
        )

    print(table)
    legend = (
        f"[{PALETTE['meta']}]mix:[/{PALETTE['meta']}] "
        f"[{PALETTE['info']}]correction[/{PALETTE['info']}] / "
        f"[{PALETTE['deny']}]rejection[/{PALETTE['deny']}] / "
        f"[{PALETTE['deny_bold']}]failure[/{PALETTE['deny_bold']}] / "
        f"[{PALETTE['learn']}]positive-redirect[/{PALETTE['learn']}] / "
        f"[{PALETTE['attention']}]scope-narrow[/{PALETTE['attention']}] / "
        f"[{PALETTE['meta']}]non-pushback[/{PALETTE['meta']}]"
    )
    print(legend)
