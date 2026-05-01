from __future__ import annotations

"""Terminal-facing prompts and rendering helpers for `hw run`."""

from contextlib import contextmanager
import re
import threading
import textwrap

from rich import print
from rich.console import Console
from rich.prompt import Prompt

from ..policy import PolicyDecision
from ..schema import IntentDeclaration, WorkflowPhase
from ..trust_db import PolicyHistory

_CONSOLE = Console()
_MODEL_STATUS_PHRASES: dict[str, tuple[str, ...]] = {
    "intent": ("reasoning", "mapping constraints", "planning"),
    "updates": ("drafting edits", "checking scope", "preparing patch"),
    "rules": ("compiling rule", "classifying governance", "extracting guidance"),
    "preferences": ("learning preferences", "updating guidance", "refining autonomy"),
}


@contextmanager
def _model_status(stage: str):
    phrases = _MODEL_STATUS_PHRASES.get(stage, ("working",))
    base_text = f"[cyan]Hedwig[/cyan] [dim]{phrases[0]}[/dim]"
    stop_event = threading.Event()
    try:
        status = _CONSOLE.status(base_text, spinner="dots", transient=True)
    except TypeError:
        # Older Rich versions don't support `transient`.
        status = _CONSOLE.status(base_text, spinner="dots")

    def _animate() -> None:
        index = 1
        while not stop_event.wait(0.8):
            phrase = phrases[index % len(phrases)]
            status.update(f"[cyan]Hedwig[/cyan] [dim]{phrase}[/dim]")
            index += 1

    with status:
        worker = threading.Thread(target=_animate, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop_event.set()
            worker.join(timeout=0.2)


def _render_file_list(files: list[str]) -> None:
    for path in files:
        print(f"  - {path}")


def _prompt_optional_feedback(prompt_text: str) -> str | None:
    note = Prompt.ask(prompt_text, default="").strip()
    return note or None


def _prompt_approval(
    stage: str,
    files: list[str],
    allow_remember: bool,
) -> tuple[bool, bool, str | None]:
    print(f"\n[bold]Approval required ({stage})[/bold]")
    print("Agent requests to modify:")
    _render_file_list(files)
    choices = ["a", "d"]
    if allow_remember:
        choices.insert(1, "r")
        prompt = "Approve once (a), approve & remember (r), deny (d)"
    else:
        prompt = "Approve once (a), deny (d)"
    response = Prompt.ask(prompt, choices=choices)
    if response == "a":
        return True, False, None
    if response == "r":
        note = _prompt_optional_feedback("Optional note for future autonomy decisions")
        return True, True, note
    note = _prompt_optional_feedback("Optional reason for denial")
    return False, False, note


def _prompt_read(files: list[str], reason: str | None) -> tuple[bool, bool, str | None]:
    print("\n[bold]Read request[/bold]")
    if reason:
        print(f"Reason: {reason}")
    print("Agent requests to read:")
    _render_file_list(files)
    response = Prompt.ask(
        "Approve once (a), approve & remember (r), or deny (d)",
        choices=["a", "r", "d"],
    )
    if response == "a":
        return True, False, None
    if response == "r":
        return True, True, None
    note = _prompt_optional_feedback("Optional reason for denying this read")
    return False, False, note


def _render_intent_summary(declaration: IntentDeclaration) -> None:
    print("\n[bold]Intent summary[/bold]")
    print(f"Task summary: {declaration.task_summary}")
    print(f"Planned actions: {', '.join(declaration.planned_actions) or 'none'}")
    if declaration.notes:
        print(f"Plan: {declaration.notes}")
    if declaration.expected_change_types:
        print(f"Expected change types: {', '.join(declaration.expected_change_types)}")
    if declaration.requirements_covered:
        print("Requirements covered:")
        _render_file_list(declaration.requirements_covered)
    if declaration.potential_deviations:
        print("Potential deviations:")
        _render_file_list(declaration.potential_deviations)
    print("Planned files:")
    _render_file_list(declaration.planned_files)


def _prompt_plan_checkpoint(
    declaration: IntentDeclaration,
    reasons: tuple[str, ...],
) -> tuple[str, str | None]:
    print("\n[bold]Plan checkpoint required[/bold]")
    _render_intent_summary(declaration)
    if reasons:
        print("Why this needs explicit plan approval:")
        for reason in reasons:
            print(f"  - {reason}")
    decision = Prompt.ask(
        "Approve plan (a), request revision (v), or deny task (d)",
        choices=["a", "v", "d"],
    )
    if decision == "a":
        return "approve", None
    if decision == "v":
        note = _prompt_optional_feedback("What should the plan change?")
        return "revise", note
    note = _prompt_optional_feedback("Optional reason for denying this task")
    return "deny", note


def _prompt_permanent(files: list[str]) -> bool:
    print("\n[bold]Grant permanent access?[/bold]")
    print("You've approved these files multiple times:")
    _render_file_list(files)
    response = Prompt.ask("Always approve changes to these files? (y/n)", choices=["y", "n"], default="n")
    return response == "y"


def _confirm_read_missing(missing_files: list[str]) -> bool:
    print("\n[bold yellow]Files don't exist yet[/bold yellow]")
    _render_file_list(missing_files)
    response = Prompt.ask(
        "Files don't exist yet; proceed with create workflow? (a)pprove/(d)eny",
        choices=["a", "d"],
    )
    return response == "a"


def _confirm_create_files(missing_files: list[str]) -> bool:
    print("\n[bold yellow]Patch will create new files[/bold yellow]")
    _render_file_list(missing_files)
    response = Prompt.ask("Allow new file creation? (a)pprove/(d)eny", choices=["a", "d"])
    return response == "a"


_ACTION_LABELS: dict[str, str] = {
    "deny": "denied",
    "check_in": "needs approval",
    "proceed": "approved",
    "proceed_flag": "approved (flagged)",
}

_APPROVALS_RE = re.compile(r"([\d.]+)\s*weighted approvals")


def _user_friendly_reason(policy: PolicyDecision) -> str:
    """Translate the primary policy reason into plain language."""
    if not policy.reasons:
        return ""
    for reason in policy.reasons:
        if reason.startswith("~guidance:"):
            return reason.split(":", 1)[1]
    first = policy.reasons[0]
    if first.startswith("hard constraint: always_deny"):
        return "blocked by your rule"
    if first.startswith("hard constraint: always_check_in"):
        return "your rule: always check in"
    if first.startswith("hard constraint: always_allow"):
        return "your rule: always allow"
    if "active write lease" in first or "active read lease" in first:
        return "permanent access granted"
    if first.startswith("adaptive policy disabled"):
        return "policy disabled"
    match = _APPROVALS_RE.search(first)
    if match:
        count = int(float(match.group(1)))
        return f"approved {count} times before" if count else "no prior approvals"
    if policy.action == "check_in" and policy.score == 0.0:
        return "first time accessing this file"
    for reason in policy.reasons:
        if "-risk:new file" in reason:
            return "new file"
        if "-risk:security sensitive" in reason:
            return "security-sensitive file"
        if "-risk:large diff" in reason:
            return "large change"
        if "-risk:interface change" in reason:
            return "API/interface change"
    for reason in policy.reasons:
        if "-risk:multi-file blast radius" in reason or "-risk:large multi-file action" in reason:
            return "affects multiple files"
    return ""


def _render_policy_snapshot(
    *,
    stage: str,
    files: list[str],
    histories: dict[str, PolicyHistory],
    policies: dict[str, PolicyDecision],
    force: bool = False,
) -> None:
    """Render per-file policy decisions.

    Suppressed when all files are silently auto-approved (nothing for the
    developer to act on). Pass force=True to always show (e.g. check-in paths).
    """
    if not files:
        return
    actions = {policies[p].action for p in files if p in policies}
    all_silent = actions <= {"proceed"}
    if all_silent and not force:
        return
    print(f"\n[bold]Policy ({stage})[/bold]")
    for path in files:
        policy = policies.get(path)
        if policy is None:
            continue
        label = _ACTION_LABELS.get(policy.action, policy.action)
        reason = _user_friendly_reason(policy)
        if reason:
            print(f"  {path}: {label} ({reason})")
        else:
            print(f"  {path}: {label}")


def _show_system_prompt(phase: WorkflowPhase, prompt_text: str) -> None:
    print(f"\n[bold]System prompt ({phase})[/bold]")
    print(prompt_text)


def _render_autonomy_rationale(stage: str, rationale: str | None) -> None:
    if not rationale:
        return
    print(f"[dim]Autonomy rationale ({stage}): {rationale}[/dim]")


def _render_history_context(
    stage: str,
    quantitative: str | None,
    qualitative: str | None,
) -> None:
    if not quantitative and not qualitative:
        return
    if quantitative:
        summary = textwrap.shorten(quantitative, width=110, placeholder="...")
        print(f"[dim]Reduced friction ({stage}): {summary}[/dim]")
    if qualitative:
        summary = textwrap.shorten(qualitative, width=140, placeholder="...")
        if summary.startswith("guidance: "):
            summary = f"Retrieved guidance: {summary.removeprefix('guidance: ')}"
        elif summary.startswith("feedback: "):
            summary = f"Retrieved feedback: {summary.removeprefix('feedback: ')}"
        elif summary.startswith("related note: "):
            summary = f"Retrieved note: {summary.removeprefix('related note: ')}"
        else:
            summary = f"Retrieved context: {summary}"
        print(f"[dim]{summary}[/dim]")


def _render_auto_approve_summary(
    stage: str,
    quantitative: str | None,
    qualitative: str | None,
    rationale: str | None,
) -> None:
    """Single compact line for auto-approved actions — replaces the two separate dim lines."""
    parts: list[str] = []
    if quantitative:
        parts.append(textwrap.shorten(quantitative, width=90, placeholder="..."))
    if qualitative:
        qual = qualitative
        for prefix in ("guidance: ", "feedback: ", "related note: "):
            if qual.startswith(prefix):
                inner = qual[len(prefix):]
                qual = f'guidance: "{textwrap.shorten(inner, width=70, placeholder="...")}"'
                break
        parts.append(qual)
    elif rationale:
        parts.append(rationale)
    if parts:
        print(f"[dim]{stage}: {' -- '.join(parts[:2])}[/dim]")


def _summarize_autonomy_rationale(
    *,
    files: list[str],
    policies: dict[str, PolicyDecision],
    milestone_reasons: tuple[str, ...] = (),
) -> str | None:
    if milestone_reasons:
        return "; ".join(milestone_reasons[:2])
    if not files:
        return None
    checkin_reasons = []
    auto_reasons = []
    for path in files:
        policy = policies.get(path)
        if policy is None:
            continue
        reason = _user_friendly_reason(policy)
        if not reason:
            continue
        if policy.action == "check_in":
            checkin_reasons.append(reason)
        else:
            auto_reasons.append(reason)
    reasons = checkin_reasons or auto_reasons
    if not reasons:
        return None
    unique = list(dict.fromkeys(reasons))
    if len(unique) == 1:
        return unique[0]
    return ", ".join(unique[:2])
