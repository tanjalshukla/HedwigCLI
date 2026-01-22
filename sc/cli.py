from __future__ import annotations

import difflib
import hashlib
import json
import time
from pathlib import Path

import boto3
import typer
from botocore.exceptions import ClientError
from rich import print
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table

from .agent_client import ASK_SYSTEM_PROMPT, ClaudeClient, RUN_SYSTEM_PROMPT
from .config import SAConfig, config_dir, default_region, env_model_id, load_config, save_config
from .patch import (
    PatchValidationError,
    validate_touched_files,
)
from .policy import within_scope_budget
from .repo import RepoError, get_repo_root
from .schema import IntentDeclaration, ReadRequest
from .session import ClaudeSession
from .trust_db import TrustDB

app = typer.Typer(add_completion=False)

def _resolve_config(repo_root: Path, model_id: str | None, region: str | None) -> SAConfig:
    config = load_config(repo_root)
    if config is None:
        env_model = env_model_id()
        if not model_id and not env_model:
            raise typer.BadParameter("Missing model id. Run `sc init` or pass --model-id.")
        return SAConfig(
            model_id=model_id or env_model,  # type: ignore[arg-type]
            aws_region=region or default_region(),
        )
    if model_id:
        config.model_id = model_id
    if region:
        config.aws_region = region
    return config


def _render_file_list(files: list[str]) -> None:
    for path in files:
        print(f"  - {path}")


def _prompt_approval(stage: str, files: list[str], allow_remember: bool) -> tuple[bool, bool]:
    print(f"\n[bold]Approval required ({stage})[/bold]")
    print("Agent requests to modify:")
    _render_file_list(files)
    choices = ["a", "d"]
    if allow_remember:
        choices.insert(1, "r")
        prompt = "Approve once (a), approve & remember (r), deny (d)"
    else:
        prompt = "Approve once (a), deny (d)"
    response = Prompt.ask(prompt, choices=choices, default="d")
    if response == "a":
        return True, False
    if response == "r":
        return True, True
    return False, False


def _prompt_read(files: list[str], reason: str | None) -> bool:
    print("\n[bold]Read request[/bold]")
    if reason:
        print(f"Reason: {reason}")
    print("Agent requests to read:")
    _render_file_list(files)
    response = Prompt.ask("Approve (a) or deny (d)", choices=["a", "d"], default="d")
    return response == "a"


def _prompt_permanent(files: list[str]) -> bool:
    print("\n[bold]Grant indefinite permission?[/bold]")
    print("These files have been approved repeatedly:")
    _render_file_list(files)
    response = Prompt.ask("Allow auto-apply for future changes (y/n)", choices=["y", "n"], default="n")
    return response == "y"


def _append_file_context(
    session: ClaudeSession, files: list[str], repo_root: Path, max_chars: int
) -> None:
    blocks: list[str] = []
    for path in files:
        file_path = repo_root / path
        try:
            content = file_path.read_text()
        except Exception:
            content = ""
        if len(content) > max_chars:
            head = content[: max_chars // 2]
            tail = content[-max_chars // 2 :]
            content = f"{head}\n\n... [truncated] ...\n\n{tail}"
        blocks.append(f"FILE: {path}\n-----\n{content}\n-----")
    if blocks:
        session.add_user("Requested file contents:\n" + "\n\n".join(blocks))


def _confirm_read_missing(missing_files: list[str]) -> bool:
    print("\n[bold yellow]Read request includes missing files[/bold yellow]")
    _render_file_list(missing_files)
    response = Prompt.ask("Continue anyway? (a)pprove/(d)eny", choices=["a", "d"], default="d")
    return response == "a"


def _confirm_create_files(missing_files: list[str]) -> bool:
    print("\n[bold yellow]Patch will create new files[/bold yellow]")
    _render_file_list(missing_files)
    response = Prompt.ask("Allow new file creation? (a)pprove/(d)eny", choices=["a", "d"], default="d")
    return response == "a"


def _read_file_context(repo_root: Path, files: list[str], max_chars: int) -> dict[str, str]:
    context: dict[str, str] = {}
    for path in files:
        file_path = repo_root / path
        try:
            content = file_path.read_text()
        except Exception:
            content = ""
        if len(content) > max_chars:
            head = content[: max_chars // 2]
            tail = content[-max_chars // 2 :]
            content = f"{head}\n\n... [truncated] ...\n\n{tail}"
        context[path] = content
    return context


def _build_patch_from_updates(
    repo_root: Path, updates: dict[str, str]
) -> tuple[str, list[str]]:
    diffs: list[str] = []
    touched: list[str] = []
    for path, new_content in updates.items():
        file_path = repo_root / path
        old_content = ""
        if file_path.exists():
            try:
                old_content = file_path.read_text()
            except Exception:
                old_content = ""
        if new_content == old_content:
            continue
        fromfile = "/dev/null" if not file_path.exists() else f"a/{path}"
        tofile = f"b/{path}"
        old_lines = old_content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                old_lines, new_lines, fromfile=fromfile, tofile=tofile, lineterm=""
            )
        )
        if diff_lines:
            diffs.append("\n".join(diff_lines))
            touched.append(path)
    patch_text = "\n".join(diffs).strip()
    if patch_text and not patch_text.endswith("\n"):
        patch_text += "\n"
    return patch_text, touched


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

@app.command()
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
    except ClientError as exc:
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


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question for the model."),
    model_id: str = typer.Option(None, "--model-id", help="Bedrock inference profile ID/ARN."),
    region: str = typer.Option(None, "--region", help="AWS region for Bedrock."),
    files: list[str] = typer.Option(None, "--file", "-f", help="Repo-relative file to include as context."),
):
    """Ask a question without making any code changes."""
    repo_root = None
    try:
        repo_root = get_repo_root()
    except RepoError:
        repo_root = None

    if files and repo_root is None:
        print("[red]Cannot resolve repo root for file context.[/red]")
        raise typer.Exit(code=1)

    if repo_root is None:
        try:
            config = _resolve_config(Path.cwd(), model_id, region)
        except typer.BadParameter as exc:
            print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    else:
        try:
            config = _resolve_config(repo_root, model_id, region)
        except typer.BadParameter as exc:
            print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

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


@app.command()
def init(
    model_id: str = typer.Option(..., "--model-id", help="Bedrock inference profile ID/ARN."),
    region: str = typer.Option(None, "--region", help="AWS region for Bedrock."),
):
    """Initialize .sc config and trust DB."""
    try:
        repo_root = get_repo_root()
    except RepoError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    config = SAConfig(model_id=model_id, aws_region=region or default_region())
    config_path = save_config(repo_root, config)
    TrustDB(config_dir(repo_root) / "trust.db")

    print("[green]Initialized Semantic Autonomy config.[/green]")
    print(f"Config: {config_path}")


@app.command()
def run(
    task: str = typer.Argument(..., help="Task for the agent."),
    model_id: str = typer.Option(None, "--model-id", help="Bedrock inference profile ID/ARN."),
    region: str = typer.Option(None, "--region", help="AWS region for Bedrock."),
    remember: bool = typer.Option(True, "--remember/--no-remember", help="Allow remember leases."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff but do not apply patch."),
    show_intent: bool = typer.Option(False, "--show-intent", help="Display intent summary and plan."),
    permanent_threshold: int | None = typer.Option(
        None,
        "--permanent-threshold",
        help="Approvals required before offering permanent permission.",
    ),
):
    """Run the agent with intent gating and patch approval."""
    try:
        repo_root = get_repo_root()
    except RepoError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    try:
        config = _resolve_config(repo_root, model_id, region)
    except typer.BadParameter as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    trust_db = TrustDB(config_dir(repo_root) / "trust.db")
    threshold = permanent_threshold if permanent_threshold is not None else config.permanent_approval_threshold
    client = ClaudeClient(model_id=config.model_id, region=config.aws_region)
    session = ClaudeSession(RUN_SYSTEM_PROMPT)

    declaration: IntentDeclaration | None = None
    while declaration is None:
        try:
            print("[cyan]Calling model for intent...[/cyan]")
            response = client.declare_intent(
                session,
                task=task,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
            )
        except Exception as exc:
            print("[red]Failed to obtain valid intent declaration.[/red]")
            print(str(exc))
            raise typer.Exit(code=1)

        if isinstance(response, ReadRequest):
            requested = response.files
            if not requested:
                print("[red]Read request contained no files.[/red]")
                raise typer.Exit(code=1)
            missing_reads = [path for path in requested if not (repo_root / path).exists()]
            if missing_reads and not _confirm_read_missing(missing_reads):
                print("[yellow]Read request denied.[/yellow]")
                raise typer.Exit(code=0)

            active_reads = trust_db.active_read_leases(str(repo_root), requested)
            untrusted_reads = [path for path in requested if path not in active_reads]
            if untrusted_reads:
                approved = _prompt_read(untrusted_reads, response.reason)
                trust_db.record_decision(
                    str(repo_root),
                    task,
                    "read",
                    approved=approved,
                    remembered=False,
                    planned_files=requested,
                    touched_files=requested,
                )
                if not approved:
                    print("[yellow]Read request denied.[/yellow]")
                    raise typer.Exit(code=0)
                trust_db.add_permanent_read_leases(
                    str(repo_root),
                    requested,
                    source="user_permanent",
                )
            else:
                trust_db.record_decision(
                    str(repo_root),
                    task,
                    "read",
                    approved=True,
                    remembered=False,
                    planned_files=requested,
                    touched_files=requested,
                )

            _append_file_context(session, requested, repo_root, config.read_max_chars)
            continue

        declaration = response

    planned_files = declaration.planned_files
    trust_db.record_decision(
        str(repo_root),
        task,
        "declare",
        approved=True,
        remembered=False,
        planned_files=planned_files,
    )
    if show_intent:
        print("\n[bold]Intent summary[/bold]")
        print(f"Task summary: {declaration.task_summary}")
        print(f"Planned actions: {', '.join(declaration.planned_actions) or 'none'}")
        if declaration.notes:
            print(f"Plan: {declaration.notes}")
        print("Planned files:")
        _render_file_list(planned_files)

    file_context = _read_file_context(repo_root, planned_files, config.read_max_chars)
    file_hashes = {}
    for path in planned_files:
        try:
            current = (repo_root / path).read_text()
        except Exception:
            current = ""
        file_hashes[path] = hashlib.sha256(current.encode("utf-8")).hexdigest()
    allowed_files = set(planned_files)
    update_error: str | None = None
    patch_text = ""
    touched_files: list[str] = []
    updates: dict[str, str] = {}
    for _ in range(2):
        try:
            print("[cyan]Calling model for file updates...[/cyan]")
            updates = client.generate_updates(
                session,
                declaration,
                file_context=file_context,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
                repair_hint=update_error,
            )
            extra = set(updates.keys()) - allowed_files
            if extra:
                update_error = f"Updates include unapproved files: {sorted(extra)}"
                continue
            patch_text, touched_files = _build_patch_from_updates(repo_root, updates)
            if not patch_text or not touched_files:
                update_error = "No changes found in updates."
                continue
            try:
                validate_touched_files(repo_root, touched_files, allowed_files)
            except PatchValidationError as exc:
                update_error = str(exc)
                continue
            update_error = None
            break
        except Exception as exc:
            update_error = str(exc)
            continue

    if update_error:
        print(f"[red]{update_error}[/red]")
        raise typer.Exit(code=1)

    print("\n[bold]Proposed patch[/bold]")
    print(Syntax(patch_text, "diff", theme="ansi_dark", word_wrap=False))

    new_files = [path for path in touched_files if not (repo_root / path).exists()]
    if new_files and not _confirm_create_files(new_files):
        print("[yellow]Patch denied.[/yellow]")
        raise typer.Exit(code=0)

    active_apply = trust_db.active_leases(str(repo_root), touched_files)
    auto_approved_apply = len(active_apply) == len(touched_files)

    if auto_approved_apply:
        print("[green]Apply auto-approved via active leases.[/green]")
        trust_db.record_decision(
            str(repo_root),
            task,
            "apply",
            approved=True,
            remembered=False,
            planned_files=planned_files,
            touched_files=touched_files,
        )
    else:
        allow_remember = remember and within_scope_budget(touched_files, config.scope_budget_files)
        approved, remembered = _prompt_approval("apply", touched_files, allow_remember)
        trust_db.record_decision(
            str(repo_root),
            task,
            "apply",
            approved=approved,
            remembered=remembered,
            planned_files=planned_files,
            touched_files=touched_files,
        )
        if not approved:
            print("[yellow]Patch denied.[/yellow]")
            raise typer.Exit(code=0)
        if remembered:
            trust_db.add_leases(
                str(repo_root),
                touched_files,
                ttl_hours=config.lease_ttl_hours,
                source="user_remember",
            )
        if remember and threshold > 0:
            counts = trust_db.approved_apply_counts(str(repo_root), touched_files)
            active_for_prompt = trust_db.active_leases(str(repo_root), touched_files)
            eligible = [
                path
                for path in touched_files
                if counts.get(path, 0) >= threshold
                and not (path in active_for_prompt and active_for_prompt[path].expires_at is None)
            ]
            if eligible and _prompt_permanent(eligible):
                trust_db.add_permanent_leases(
                    str(repo_root),
                    eligible,
                    source="user_permanent",
                )

    if dry_run:
        print("[yellow]Dry run enabled: patch not applied.[/yellow]")
        return

    for path in touched_files:
        file_path = repo_root / path
        try:
            current = file_path.read_text()
        except Exception:
            current = ""
        current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if current_hash != file_hashes.get(path):
            print(f"[red]File changed since model response: {path}[/red]")
            raise typer.Exit(code=1)

    for path, content in updates.items():
        if path not in touched_files:
            continue
        file_path = repo_root / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)

    print("[green]Patch applied successfully.[/green]")


@app.command()
def leases(
    json_out: bool = typer.Option(False, "--json", help="Output leases as JSON."),
):
    """List active leases for this repo."""
    try:
        repo_root = get_repo_root()
    except RepoError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    trust_db = TrustDB(config_dir(repo_root) / "trust.db")
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


@app.command()
def revoke(
    path: str | None = typer.Argument(None, help="Repo-relative file path to revoke."),
    all: bool = typer.Option(False, "--all", help="Revoke all leases for this repo."),
):
    """Revoke leases for a file (or all)."""
    if not path and not all:
        print("[red]Provide a path or --all.[/red]")
        raise typer.Exit(code=1)

    try:
        repo_root = get_repo_root()
    except RepoError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    trust_db = TrustDB(config_dir(repo_root) / "trust.db")
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
