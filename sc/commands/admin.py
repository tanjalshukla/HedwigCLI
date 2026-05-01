from __future__ import annotations

import json
from pathlib import Path

import boto3
import typer
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from rich import print
from rich.prompt import Prompt
from rich.table import Table

from ..agent_client import ASK_SYSTEM_PROMPT, ClaudeClient, RUN_SYSTEM_PROMPT
from ..autonomy import preferences_from_model_payload
from ..cli_shared import read_file_context as _read_file_context
from ..cli_shared import resolve_config as _resolve_config
from ..commands.shared import open_trust_db, require_repo_root, try_repo_root
from ..config import (
    SAConfig,
    default_region,
    env_model_id,
    load_config,
    normalize_autonomy_mode,
    save_config,
)
from ..constraints import ParseResult, parse_constraints_file
from ..session import ClaudeSession
from ..trust_db import HardConstraint
from ..run.ui import _model_status


def _resolve_config_or_exit(
    repo_root: Path,
    model_id: str | None,
    region: str | None,
):
    try:
        return _resolve_config(repo_root, model_id, region)
    except typer.BadParameter as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


def doctor(
    model_id: str = typer.Option(
        None,
        "--model-id",
        help="Bedrock inference profile ID/ARN.",
    ),
    region: str = typer.Option(None, "--region", help="AWS region for Bedrock."),
    prompt: str = typer.Option(
        "Say OK and nothing else.",
        "--prompt",
        help="Prompt sent to the model.",
    ),
):
    """Verify AWS identity and Bedrock invocation."""
    region = region or default_region()
    print(f"[bold]Region:[/bold] {region}")

    try:
        sts = boto3.client("sts", region_name=region)
        ident = sts.get_caller_identity()
        print("[bold]AWS Identity:[/bold]")
        print(json.dumps(ident, indent=2))
    except (ClientError, BotoCoreError, NoCredentialsError) as exc:
        print("[red]STS call failed.[/red]")
        print(str(exc))
        raise typer.Exit(code=1)

    if not model_id:
        model_id = env_model_id()
    if not model_id:
        print("\n[yellow]No --model-id provided.[/yellow]")
        raise typer.Exit(code=0)

    try:
        client = ClaudeClient(model_id=model_id, region=region)
        session = ClaudeSession(RUN_SYSTEM_PROMPT)
        session.add_user(prompt)
        response = client._call(session, max_tokens=16, temperature=0)
        print("\n[bold green]Bedrock Claude OK[/bold green]")
        print("[bold]Model output:[/bold]")
        print(response)
    except Exception as exc:
        print("\n[red]Bedrock invoke failed.[/red]")
        print(str(exc))
        raise typer.Exit(code=2)


def ask(
    question: str = typer.Argument(..., help="Question for the model."),
    model_id: str = typer.Option(None, "--model-id", help="Bedrock inference profile ID/ARN."),
    region: str = typer.Option(None, "--region", help="AWS region for Bedrock."),
    files: list[str] = typer.Option(None, "--file", "-f", help="Repo-relative file to include as context."),
):
    """Ask a question without making any code changes."""
    repo_root = try_repo_root()

    if files and repo_root is None:
        print("[red]Cannot resolve repo root for file context.[/red]")
        raise typer.Exit(code=1)

    config = _resolve_config_or_exit(
        repo_root if repo_root is not None else Path.cwd(),
        model_id,
        region,
    )

    client = ClaudeClient(model_id=config.model_id, region=config.aws_region)
    session = ClaudeSession(ASK_SYSTEM_PROMPT)

    context_text = ""
    if files and repo_root is not None:
        context_map = _read_file_context(repo_root, files, max_chars=config.read_max_chars)
        blocks = []
        for path, content in context_map.items():
            blocks.append(f"FILE: {path}\n-----\n{content}\n-----")
        context_text = "\n\n".join(blocks)

    if context_text:
        session.add_user(f"Question: {question}\n\nContext:\n{context_text}")
    else:
        session.add_user(question)

    try:
        response = client._call(session, max_tokens=config.max_tokens, temperature=config.temperature)
    except Exception as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    print(response)


def init(
    model_id: str = typer.Option(..., "--model-id", help="Bedrock inference profile ID/ARN."),
    region: str = typer.Option(None, "--region", help="AWS region for Bedrock."),
):
    """Initialize .sc config and trust DB."""
    repo_root = require_repo_root()

    config = SAConfig(model_id=model_id, aws_region=region or default_region())
    config_path = save_config(repo_root, config)
    trust_db = open_trust_db(repo_root)

    # Pre-build the warm-start classifier so the first `hw run` has no startup lag.
    from ..ml_policy import build_warm_start_classifier
    if trust_db.load_policy_model(str(repo_root)) is None:
        classifier = build_warm_start_classifier()
        trust_db.save_policy_model(str(repo_root), classifier)

    print("[green]Initialized Hedwig config.[/green]")
    print(f"Config: {config_path}")


def set_mode(
    mode: str = typer.Argument(
        ...,
        help="Autonomy mode: strict, balanced, milestone, or autonomous.",
    ),
):
    """Set the user-facing autonomy mode in .sc/config.json."""
    repo_root = require_repo_root()
    config = load_config(repo_root)
    if config is None:
        print("[red]Config not found. Run `hw init` first.[/red]")
        raise typer.Exit(code=1)
    normalized = normalize_autonomy_mode(mode)
    if normalized != mode.strip().lower():
        print("[yellow]Unknown mode. Use strict, balanced, milestone, or autonomous.[/yellow]")
        raise typer.Exit(code=1)
    config.autonomy_mode = normalized
    save_config(repo_root, config)
    print(f"[green]Autonomy mode set to {normalized}.[/green]")


def set_verification_cmd(
    command: str | None = typer.Argument(
        None,
        help="Verification command to run after apply (e.g. 'pytest -q').",
    ),
    clear: bool = typer.Option(False, "--clear", help="Clear custom verification command."),
):
    """Configure post-apply verification command in .sc/config.json."""
    repo_root = require_repo_root()

    config = load_config(repo_root)
    if config is None:
        print("[red]Config not found. Run `hw init` first.[/red]")
        raise typer.Exit(code=1)

    if clear:
        config.verification_command = None
        save_config(repo_root, config)
        print("[green]Cleared verification command.[/green]")
        return

    if not command or not command.strip():
        print("[red]Provide a command or use --clear.[/red]")
        raise typer.Exit(code=1)

    config.verification_command = command.strip()
    save_config(repo_root, config)
    print(f"[green]Verification command set:[/green] {config.verification_command}")


def import_rules(
    files: list[str] = typer.Argument(..., metavar="FILE...", help="Rule file paths to import."),
):
    """Import hard constraints and behavioral guidelines from markdown rule files."""
    repo_root = require_repo_root()

    targets = [Path(item) for item in files]
    trust_db = open_trust_db(repo_root)
    total_imported = 0
    total_guidelines = 0
    for target in targets:
        path = target if target.is_absolute() else (repo_root / target)
        if not path.exists():
            print(f"[yellow]Skipping missing file:[/yellow] {path}")
            continue
        try:
            parsed = parse_constraints_file(path)
        except Exception as exc:
            print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        imported = trust_db.replace_constraints(
            str(repo_root),
            source=path.name,
            constraints=parsed.constraints,
        )
        guideline_count = trust_db.replace_behavioral_guidelines(
            str(repo_root),
            source=path.name,
            guidelines=parsed.behavioral_guidelines,
        )
        total_imported += imported
        total_guidelines += guideline_count
        print(f"[green]Imported {imported} constraints from {path.name}.[/green]")
        print(f"[green]Imported {guideline_count} behavioral guidelines from {path.name}.[/green]")
        if parsed.unresolved_lines:
            print(f"[yellow]{len(parsed.unresolved_lines)} lines were ambiguous and handled conservatively.[/yellow]")

    print(f"[bold]Total constraints imported:[/bold] {total_imported}")
    print(f"[bold]Total behavioral guidelines imported:[/bold] {total_guidelines}")


def add_rule(
    rule: str = typer.Argument(..., help="Natural-language rule to compile into enforced constraints and/or behavioral guidance."),
    source: str = typer.Option("manual_rule", "--source", help="Source label stored with the compiled constraint."),
    model_id: str = typer.Option(None, "--model-id", help="Bedrock inference profile ID/ARN."),
    region: str = typer.Option(None, "--region", help="AWS region for Bedrock."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
):
    """Compile a natural-language rule into enforced constraints and/or behavioral guidance."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    config = _resolve_config_or_exit(repo_root, model_id, region)
    try:
        with _model_status("rules"):
            parsed = _compile_rule_with_model(
                repo_root=repo_root,
                rule=rule,
                source=source,
                model_id=config.model_id,
                region=config.aws_region,
            )
    except Exception as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    if not parsed.constraints and not parsed.behavioral_guidelines:
        print("[red]Could not compile a usable rule from that input.[/red]")
        if parsed.unresolved_lines:
            print("[yellow]The rule was too ambiguous to enforce or inject safely.[/yellow]")
        raise typer.Exit(code=1)

    if parsed.constraints:
        table = Table(title="Proposed Hard Constraints")
        table.add_column("Pattern")
        table.add_column("Policy")
        table.add_column("Source")
        for item in parsed.constraints:
            table.add_row(item.path_pattern, _constraint_display(item), item.source)
        print(table)
    if parsed.behavioral_guidelines:
        table = Table(title="Proposed Behavioral Guidelines")
        table.add_column("Guideline")
        table.add_column("Source")
        for guideline in parsed.behavioral_guidelines:
            table.add_row(guideline, source)
        print(table)
    if parsed.unresolved_lines:
        print("[yellow]Unresolved portions (not persisted):[/yellow]")
        for line in parsed.unresolved_lines:
            print(f"[yellow]- {line}[/yellow]")

    if not yes:
        confirmed = Prompt.ask("Add these compiled rules", choices=["y", "n"], default="y")
        if confirmed != "y":
            print("[yellow]No rules added.[/yellow]")
            raise typer.Exit(code=0)

    inserted = trust_db.add_constraints(str(repo_root), parsed.constraints)
    guideline_count = trust_db.add_behavioral_guidelines(
        str(repo_root),
        source=source,
        guidelines=parsed.behavioral_guidelines,
    )
    with _model_status("preferences"):
        learned_preferences = _learn_preferences_from_guidelines(
            trust_db=trust_db,
            repo_root=repo_root,
            guidelines=parsed.behavioral_guidelines,
            model_id=config.model_id,
            region=config.aws_region,
        )
    print(f"[green]Added {inserted} hard constraint(s).[/green]")
    print(f"[green]Added {guideline_count} behavioral guideline(s).[/green]")
    if learned_preferences:
        print(f"[green]Updated adaptive guidance:[/green] {', '.join(learned_preferences)}")


def _repo_inventory(repo_root: Path, *, limit: int = 80) -> list[str]:
    items: list[str] = []
    ignored = {".git", ".venv", ".sc", "__pycache__"}
    for path in sorted(repo_root.rglob("*")):
        if len(items) >= limit:
            break
        if any(part in ignored for part in path.parts):
            continue
        if path.is_file():
            items.append(str(path.relative_to(repo_root)))
    return items


def _compile_rule_with_model(
    *,
    repo_root: Path,
    rule: str,
    source: str,
    model_id: str,
    region: str,
):
    client = ClaudeClient(model_id=model_id, region=region)
    compiled = client.compile_rule(rule, repo_inventory=_repo_inventory(repo_root))
    constraints = [
        HardConstraint(
            path_pattern=item.path_pattern,
            source=source,
            overridable=False,
            read_policy=item.read_policy,
            write_policy=item.write_policy,
        )
        for item in compiled.constraints
    ]
    return ParseResult(
        constraints=constraints,
        behavioral_guidelines=compiled.behavioral_guidelines,
        unresolved_lines=compiled.unresolved,
    )


def _learn_preferences_from_guidelines(
    *,
    trust_db,
    repo_root: Path,
    guidelines: list[str],
    model_id: str,
    region: str,
) -> list[str]:
    learned: list[str] = []
    if not guidelines:
        return learned
    client = ClaudeClient(model_id=model_id, region=region)
    for guideline in guidelines:
        payload = client.summarize_autonomy_feedback(guideline)
        if payload is None:
            continue
        inferred = preferences_from_model_payload(payload)
        learned.extend(trust_db.merge_autonomy_preferences(str(repo_root), inferred))
    return list(dict.fromkeys(learned))


_CONSTRAINT_LABELS: dict[str, str] = {
    "always_deny": "always deny",
    "always_check_in": "always check in",
    "always_allow": "always allow",
}


def _constraint_display(item: HardConstraint) -> str:
    read_label = _CONSTRAINT_LABELS.get(str(item.read_policy), str(item.read_policy))
    write_label = _CONSTRAINT_LABELS.get(str(item.write_policy), str(item.write_policy))
    if item.read_policy == item.write_policy:
        return read_label
    return f"read={read_label}; write={write_label}"


def rules_list(
    json_out: bool = typer.Option(False, "--json", help="Output rules as JSON."),
):
    """List all rules (file-access constraints and style guidelines)."""
    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    repo_root_str = str(repo_root)
    constraints_list = trust_db.list_constraints(repo_root_str)
    guidelines_list = trust_db.list_behavioral_guidelines(repo_root_str)

    if not constraints_list and not guidelines_list:
        print("[yellow]No rules found. Use `hw rules import <file>` to add rules.[/yellow]")
        return

    if json_out:
        payload = {
            "rules": [
                {
                    "rule": f"{item.path_pattern}: {_constraint_display(item)}",
                    "enforcement": "enforced",
                    "source": item.source,
                }
                for item in constraints_list
            ] + [
                {
                    "rule": item.guideline,
                    "enforcement": "best-effort",
                    "source": item.source,
                }
                for item in guidelines_list
            ],
        }
        print(json.dumps(payload, indent=2))
        return

    table = Table(title="Rules")
    table.add_column("Rule")
    table.add_column("Enforcement")
    table.add_column("Source")
    for item in constraints_list:
        table.add_row(
            f"{item.path_pattern}: {_constraint_display(item)}",
            "enforced",
            item.source,
        )
    for item in guidelines_list:
        table.add_row(
            item.guideline,
            "best-effort",
            item.source,
        )
    print(table)


def constraints(
    json_out: bool = typer.Option(False, "--json", help="Output constraints as JSON."),
):
    """List hard constraints imported from rule files."""
    repo_root = require_repo_root()

    trust_db = open_trust_db(repo_root)
    constraints_list = trust_db.list_constraints(str(repo_root))
    if not constraints_list:
        print("[yellow]No hard constraints found.[/yellow]")
        return

    if json_out:
        payload = [
            {
                "path_pattern": item.path_pattern,
                "read_policy": item.read_policy,
                "write_policy": item.write_policy,
                "source": item.source,
                "overridable": item.overridable,
            }
            for item in constraints_list
        ]
        print(json.dumps(payload, indent=2))
        return

    table = Table(title="Hard Constraints")
    table.add_column("Pattern")
    table.add_column("Type")
    table.add_column("Source")
    table.add_column("Overridable")
    for item in constraints_list:
        table.add_row(
            item.path_pattern,
            _constraint_display(item),
            item.source,
            "yes" if item.overridable else "no",
        )
    print(table)


def guidelines(
    json_out: bool = typer.Option(False, "--json", help="Output behavioral guidelines as JSON."),
):
    """List behavioral guidelines imported from rule files."""
    repo_root = require_repo_root()

    trust_db = open_trust_db(repo_root)
    rows = trust_db.list_behavioral_guidelines(str(repo_root))
    if not rows:
        print("[yellow]No behavioral guidelines found.[/yellow]")
        return

    if json_out:
        payload = [{"guideline": item.guideline, "source": item.source} for item in rows]
        print(json.dumps(payload, indent=2))
        return

    table = Table(title="Behavioral Guidelines")
    table.add_column("Guideline")
    table.add_column("Source")
    for item in rows:
        table.add_row(item.guideline, item.source)
    print(table)


def guidelines_suggest(
    min_count: int = typer.Option(2, "--min-count", help="Minimum repeated feedback count."),
    apply: bool = typer.Option(False, "--apply", help="Add selected suggestions to guidelines."),
    all: bool = typer.Option(False, "--all", help="Apply all suggestions (use with --apply)."),
    json_out: bool = typer.Option(False, "--json", help="Output suggestions as JSON."),
):
    """Suggest behavioral guidelines from repeated developer feedback."""
    repo_root = require_repo_root()

    repo_root_str = str(repo_root)
    trust_db = open_trust_db(repo_root)
    candidates = trust_db.guideline_candidates(repo_root_str, min_count=min_count)
    if not candidates:
        print("[yellow]No repeated feedback patterns found yet.[/yellow]")
        return

    if json_out:
        payload = [
            {"guideline": item.guideline, "count": item.count}
            for item in candidates
        ]
        print(json.dumps(payload, indent=2))
    else:
        table = Table(title="Guideline Suggestions")
        table.add_column("#")
        table.add_column("Count")
        table.add_column("Suggested Guideline")
        for idx, item in enumerate(candidates, 1):
            table.add_row(str(idx), str(item.count), item.guideline)
        print(table)

    if not apply:
        print("Use `--apply` to add suggestions as behavioral guidelines.")
        return

    selected: list[str]
    if all:
        selected = [item.guideline for item in candidates]
    else:
        choices = ",".join(str(i) for i in range(1, len(candidates) + 1))
        raw = Prompt.ask(
            f"Select suggestion numbers to apply (comma-separated from {choices})",
            default="1",
        )
        indices: list[int] = []
        for token in raw.split(","):
            token = token.strip()
            if not token.isdigit():
                continue
            value = int(token)
            if 1 <= value <= len(candidates):
                indices.append(value)
        indices = sorted(set(indices))
        if not indices:
            print("[yellow]No valid suggestions selected.[/yellow]")
            return
        selected = [candidates[i - 1].guideline for i in indices]

    inserted = trust_db.add_behavioral_guidelines(
        repo_root_str,
        source="feedback_auto",
        guidelines=selected,
    )
    print(f"[green]Added {inserted} behavioral guideline(s).[/green]")


def guidelines_clear(
    all: bool = typer.Option(False, "--all", help="Delete all behavioral guidelines for this repo."),
    source: str | None = typer.Option(None, "--source", help="Delete only behavioral guidelines from this source."),
):
    """Delete behavioral guidelines from the trust database."""
    if not all and source is None:
        print("[red]Specify --all or --source.[/red]")
        raise typer.Exit(code=1)

    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    removed = trust_db.delete_behavioral_guidelines(
        str(repo_root),
        source=None if all else source,
    )
    print(f"[green]Removed {removed} behavioral guidelines.[/green]")


def constraints_clear(
    all: bool = typer.Option(False, "--all", help="Delete all hard constraints for this repo."),
    source: str | None = typer.Option(None, "--source", help="Delete only constraints from this source file."),
    pattern: str | None = typer.Option(None, "--pattern", help="Delete only this exact path pattern."),
):
    """Delete hard constraints from the trust database."""
    if not all and source is None and pattern is None:
        print("[red]Specify --all or at least one filter (--source / --pattern).[/red]")
        raise typer.Exit(code=1)

    repo_root = require_repo_root()
    trust_db = open_trust_db(repo_root)
    removed = trust_db.delete_constraints(
        str(repo_root),
        source=None if all else source,
        path_pattern=None if all else pattern,
    )
    print(f"[green]Removed {removed} hard constraints.[/green]")


def constraints_relax(
    pattern: str = typer.Argument(..., help="Exact constraint pattern to relax (for example: demo/checkin/*)."),
    source: str | None = typer.Option(None, "--source", help="Relax only constraints from this source."),
):
    """Relax matching hard constraints to an always-allow override."""
    repo_root = require_repo_root()
    repo_root_str = str(repo_root)
    trust_db = open_trust_db(repo_root)

    existing = trust_db.list_constraints(repo_root_str)
    matched = [
        item
        for item in existing
        if item.path_pattern == pattern and (source is None or item.source == source)
    ]
    if not matched:
        print("[yellow]No matching constraints found to relax.[/yellow]")
        return

    removed = trust_db.delete_constraints(
        repo_root_str,
        source=source,
        path_pattern=pattern,
    )

    manual_constraints = [
        item
        for item in trust_db.list_constraints(repo_root_str)
        if item.source == "manual_relax" and item.path_pattern != pattern
    ]
    manual_constraints.append(
        HardConstraint(
            path_pattern=pattern,
            constraint_type="always_allow",
            source="manual_relax",
            overridable=False,
        )
    )
    trust_db.replace_constraints(
        repo_root_str,
        source="manual_relax",
        constraints=manual_constraints,
    )

    print(f"[green]Relaxed {removed} constraint(s) matching '{pattern}'.[/green]")
    if source:
        print(f"Source filter: {source}")
    print("[green]Added manual override:[/green] always_allow")
