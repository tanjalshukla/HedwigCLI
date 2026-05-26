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
from .theme import PALETTE, panel_title

_CONSOLE = Console()
# Phrase rotations for each model-call stage. Each stage now has enough
# distinct beats that the reader sees Hedwig "thinking" rather than a
# spinner stuck on one phrase. Claude-Code-inspired — one line, dim, the
# line replaces itself every ~1.5s.
_MODEL_STATUS_PHRASES: dict[str, tuple[str, ...]] = {
    "intent": (
        "reading what you asked for",
        "checking the spec",
        "looking at which files might be in scope",
        "weighing the plan",
    ),
    "updates": (
        "thinking through the change",
        "reading the files you approved",
        "drafting edits",
        "checking the patch against the plan",
        "trimming anything out-of-scope",
    ),
    "rules": (
        "parsing what you wrote",
        "deciding if this is a hard rule or guidance",
        "checking for path overlaps",
        "finalizing",
    ),
    "preferences": (
        "reading your feedback",
        "looking for patterns",
        "updating what I'm watching for",
    ),
    "reads": (
        "staging reads",
        "checking access",
    ),
    "rationale": (
        "explaining the call",
        "surfacing rationale",
    ),
}

# Thread-local current stream so callers can push thoughts from anywhere
# inside a `with _model_status(...):` block.
_ACTIVE_STATUS: dict[str, object] = {"status": None, "last_push_at": 0.0}


def push_thought(thought: str) -> None:
    """Inject a custom thought into the active model-status spinner.

    Silently no-ops if no spinner is active (e.g. called outside a
    `_model_status` context, or during tests). Useful for emitting
    context-specific reasoning from decision paths — "no hard constraint
    matched", "scorer says 0.63", etc.

    The spinner's background phrase rotation respects a recent push and
    will not clobber it for a few seconds — otherwise pushed thoughts would
    flash and vanish during long streaming calls.
    """
    import time as _time

    from .theme import PALETTE

    status = _ACTIVE_STATUS.get("status")
    if status is None:
        return
    try:
        status.update(
            f"[{PALETTE['info_bold']}]hedwig[/{PALETTE['info_bold']}]  "
            f"[{PALETTE['meta_italic']}]{thought}[/{PALETTE['meta_italic']}]"
        )
        _ACTIVE_STATUS["last_push_at"] = _time.time()
    except Exception:
        # Status may have been torn down between the check and the update.
        pass


def announce_above_spinner(line: str) -> None:
    """Print a permanent line above the active spinner, then resume spinning.

    Used to surface durable progress (e.g. "→ writing models.py") that should
    persist as an audit trail even after the spinner is torn down. No-ops if
    there is no active spinner — falls back to a normal print.
    """
    status = _ACTIVE_STATUS.get("status")
    if status is None:
        _CONSOLE.print(line)
        return
    try:
        # Rich Status / Live exposes .console.print which prints above the
        # transient spinner without disturbing it.
        status.console.print(line)
    except Exception:
        _CONSOLE.print(line)


@contextmanager
def _model_status(stage: str, initial_thought: str | None = None):
    from .theme import PALETTE

    phrases = _MODEL_STATUS_PHRASES.get(stage, ("working",))
    # If the caller gave us a context-specific opening thought, use it for the
    # first phrase. After it dwells, the choreography rotation takes over.
    opening = initial_thought if initial_thought else phrases[0]
    base_text = (
        f"[{PALETTE['info_bold']}]hedwig[/{PALETTE['info_bold']}]  "
        f"[{PALETTE['meta_italic']}]{opening}[/{PALETTE['meta_italic']}]"
    )
    stop_event = threading.Event()
    try:
        status = _CONSOLE.status(base_text, spinner="dots", transient=True)
    except TypeError:
        # Older Rich versions don't support `transient`.
        status = _CONSOLE.status(base_text, spinner="dots")

    def _animate() -> None:
        # Slower rotation gives each thought ~1.6s of dwell time, which reads
        # as thoughtful rather than frantic. If a caller has recently pushed
        # a custom thought via push_thought(), let it dwell instead of
        # clobbering it with the next rotation phrase.
        import time as _time

        index = 1
        while not stop_event.wait(1.6):
            last_push = float(_ACTIVE_STATUS.get("last_push_at") or 0.0)
            if last_push and (_time.time() - last_push) < 4.0:
                continue
            phrase = phrases[index % len(phrases)]
            status.update(
                f"[{PALETTE['info_bold']}]hedwig[/{PALETTE['info_bold']}]  "
                f"[{PALETTE['meta_italic']}]{phrase}[/{PALETTE['meta_italic']}]"
            )
            index += 1

    with status:
        _ACTIVE_STATUS["status"] = status
        worker = threading.Thread(target=_animate, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop_event.set()
            worker.join(timeout=0.2)
            _ACTIVE_STATUS["status"] = None


def _render_file_list(files: list[str]) -> None:
    for path in files:
        print(f"  [{PALETTE['info']}]·[/{PALETTE['info']}] {path}")


def _prompt_optional_feedback(prompt_text: str) -> str | None:
    # prompt_toolkit handles backspace across wrapped lines correctly,
    # but it redraws its own line — pre-printing the label via Rich gets
    # clobbered and the cursor lands on top of the label. So strip Rich
    # markup and pass the bare label to prompt_toolkit as the prompt arg.
    bare = re.sub(r"\[/?[^\]]+\]", "", prompt_text).strip()
    try:
        from prompt_toolkit import prompt as _pt_prompt
        note = _pt_prompt(f"{bare} ").strip()
    except Exception:
        from rich.console import Console as _Console
        _Console().print(f"{prompt_text} ", end="")
        note = Prompt.ask("", default="").strip()
    return note or None


def _prompt_approval(
    stage: str,
    files: list[str],
    allow_remember: bool,
    pause_reason: str | None = None,
    diff_already_shown: bool = True,
) -> tuple[bool, bool, str | None]:
    # After the diff, the developer just needs the reason + the decision.
    # Show the primary file name if not already obvious from the diff.
    print()
    if not diff_already_shown and files:
        primary = files[0] if len(files) == 1 else f"{files[0]} +{len(files)-1} more"
        print(f"[{PALETTE['info_bold']}]{primary}[/{PALETTE['info_bold']}]")
        if len(files) > 1:
            _render_file_list(files[1:])
    # pause_reason intentionally not printed here — _render_policy_snapshot
    # already shows the rationale inline on each file's line. Re-printing
    # before the prompt was duplicate noise.
    choices = ["a", "d"]
    if allow_remember:
        choices.insert(1, "r")
        prompt = (
            f"[{PALETTE['approve_bold']}]a[/{PALETTE['approve_bold']}] approve  "
            f"[{PALETTE['learn']}]r[/{PALETTE['learn']}] approve+remember  "
            f"[{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny"
        )
    else:
        prompt = (
            f"[{PALETTE['approve_bold']}]a[/{PALETTE['approve_bold']}] approve  "
            f"[{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny"
        )
    response = Prompt.ask(prompt, choices=choices, case_sensitive=False, show_choices=False)
    response = response.strip().lower()
    if response == "a":
        return True, False, None
    if response == "r":
        note = _prompt_optional_feedback(
            f"[{PALETTE['meta']}]Optional note (helps me learn your preference)[/{PALETTE['meta']}]"
        )
        return True, True, note
    note = _prompt_optional_feedback(
        f"[{PALETTE['meta']}]Optional reason for denial[/{PALETTE['meta']}]"
    )
    return False, False, note


def _prompt_read(
    files: list[str],
    reason: str | None,
) -> tuple[bool, list[str], str | None]:
    """Prompt the developer to approve a batch of read requests.

    Returns ``(approved, remember_paths, denial_feedback)``:
    * ``approved`` — True if the batch was approved (any subset of files
      may have been marked "remember"; the apply still proceeds for all).
    * ``remember_paths`` — paths the developer chose to grant a permanent
      read lease for. Subset of ``files``. Empty when nothing was marked.
    * ``denial_feedback`` — optional free-text reason captured on deny.

    For a single file, the prompt collapses to ``a / r / d``. For
    multiple files, ``r`` opens a per-file toggle so different files can
    have different lease decisions in one batch.
    """
    if reason:
        short = reason.strip().split("\n", 1)[0]
        words = short.split()
        if len(words) > 10:
            short = " ".join(words[:10]) + "…"
        print(f"[{PALETTE['meta']}]{short}[/{PALETTE['meta']}]")

    if len(files) == 1:
        response = Prompt.ask(
            f"[{PALETTE['approve_bold']}]a[/{PALETTE['approve_bold']}] approve  "
            f"[{PALETTE['learn']}]r[/{PALETTE['learn']}] approve+remember  "
            f"[{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny",
            choices=["a", "r", "d"],
            case_sensitive=False,
            show_choices=False,
        )
        response = response.strip().lower()
        if response == "a":
            return True, [], None
        if response == "r":
            return True, list(files), None
        note = _prompt_optional_feedback(
            f"[{PALETTE['meta']}]Optional reason for denying this read[/{PALETTE['meta']}]"
        )
        return False, [], note

    # Multi-file path. 'a' approves all (no remember), 'r' approves all and
    # remembers all (one-shot shortcut for trust-everything), 'p' approves
    # all and lets the developer pick which subset to remember per file,
    # 'd' denies all.
    response = Prompt.ask(
        f"[{PALETTE['approve_bold']}]a[/{PALETTE['approve_bold']}] approve all  "
        f"[{PALETTE['learn']}]r[/{PALETTE['learn']}] approve+remember all  "
        f"[{PALETTE['learn']}]p[/{PALETTE['learn']}] pick which to remember  "
        f"[{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny",
        choices=["a", "r", "p", "d"],
        case_sensitive=False,
        show_choices=False,
    )
    response = response.strip().lower()
    if response == "a":
        return True, [], None
    if response == "r":
        return True, list(files), None
    if response == "d":
        note = _prompt_optional_feedback(
            f"[{PALETTE['meta']}]Optional reason for denying this read[/{PALETTE['meta']}]"
        )
        return False, [], note
    # 'p' — per-file toggle. Default is 'no remember' to match the cautious option.
    print(f"[{PALETTE['meta']}]Mark each file to remember (y) or just approve once (N).[/{PALETTE['meta']}]")
    remember: list[str] = []
    for path in files:
        ans = Prompt.ask(
            f"  [{PALETTE['info']}]{path}[/{PALETTE['info']}] · remember?",
            choices=["y", "n"],
            default="n",
            case_sensitive=False,
            show_choices=False,
            show_default=False,
        )
        if ans.strip().lower() == "y":
            remember.append(path)
    return True, remember, None


def _render_intent_summary(declaration: IntentDeclaration) -> None:
    print()
    plan_text = declaration.notes or declaration.task_summary
    print(f"[{PALETTE['info_bold']}]Plan:[/{PALETTE['info_bold']}] {plan_text}")
    _render_file_list(declaration.planned_files)


def _prompt_plan_checkpoint(
    declaration: IntentDeclaration,
    reasons: tuple[str, ...],
) -> tuple[str, str | None]:
    print()
    _render_intent_summary(declaration)
    if reasons:
        # Single most important reason only — keep it short.
        print(f"[{PALETTE['meta']}]· {reasons[0]}[/{PALETTE['meta']}]")
    print()
    decision = Prompt.ask(
        f"[{PALETTE['approve_bold']}]a[/{PALETTE['approve_bold']}] approve  "
        f"[{PALETTE['attention']}]v[/{PALETTE['attention']}] revise  "
        f"[{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny",
        choices=["a", "v", "d"],
    )
    if decision == "a":
        return "approve", None
    if decision == "v":
        note = _prompt_optional_feedback(
            f"[{PALETTE['meta']}]What should the plan change?[/{PALETTE['meta']}]"
        )
        return "revise", note
    note = _prompt_optional_feedback(
        f"[{PALETTE['meta']}]Optional reason for denying this task[/{PALETTE['meta']}]"
    )
    return "deny", note


def _prompt_permanent(files: list[str]) -> bool:
    print()
    print(panel_title("learn", "grant permanent access?"))
    print(f"[{PALETTE['meta']}]You've approved these files multiple times:[/{PALETTE['meta']}]")
    _render_file_list(files)
    response = Prompt.ask(
        f"[{PALETTE['meta']}]Always approve changes to these files?[/{PALETTE['meta']}] (y/n)",
        choices=["y", "n"],
        default="n",
    )
    return response == "y"


def _confirm_read_missing(missing_files: list[str]) -> bool:
    print(f"\n[{PALETTE['attention_bold']}]Files don't exist yet[/{PALETTE['attention_bold']}]")
    _render_file_list(missing_files)
    response = Prompt.ask(
        f"[{PALETTE['approve_bold']}]a[/{PALETTE['approve_bold']}] create  "
        f"[{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny",
        choices=["a", "d"],
    )
    return response == "a"


def _confirm_create_files(missing_files: list[str]) -> bool:
    print(f"\n[{PALETTE['attention_bold']}]Patch will create new files[/{PALETTE['attention_bold']}]")
    _render_file_list(missing_files)
    response = Prompt.ask(
        f"[{PALETTE['approve_bold']}]a[/{PALETTE['approve_bold']}] create  "
        f"[{PALETTE['deny_bold']}]d[/{PALETTE['deny_bold']}] deny",
        choices=["a", "d"],
    )
    return response == "a"


_ACTION_LABELS: dict[str, str] = {
    "deny": "denied",
    "check_in": "needs approval",
    "proceed": "approved",
    "proceed_flag": "approved (flagged)",
}

def _user_friendly_reason(policy: "PolicyDecision") -> str:
    """Translate the primary policy reason into plain language (used for rationale summaries)."""
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
        return "adaptive scoring off — checking in by default"
    if "+history:" in first:
        return "seen before"
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
    return ""


def _trust_dot(policy: "PolicyDecision") -> str:
    """Colored confidence dot based on policy score.

    ● green  — high trust (score ≥ 0.8 or hard allow/lease)
    ● yellow — some history, borderline (0.3 ≤ score < 0.8)
    ◌ grey   — no signal / cold start
    ● red    — blocked or security flag
    """
    reasons_text = " ".join(policy.reasons)
    if any(s in reasons_text for s in ("always_deny", "security sensitive")):
        return f"[{PALETTE['deny_bold']}]●[/{PALETTE['deny_bold']}]"
    if any(s in reasons_text for s in ("always_allow", "active read lease", "active write lease")):
        return f"[{PALETTE['approve']}]●[/{PALETTE['approve']}]"
    score = policy.score or 0.0
    if score >= 0.8:
        return f"[{PALETTE['approve']}]●[/{PALETTE['approve']}]"
    if score >= 0.3:
        return f"[{PALETTE['info']}]●[/{PALETTE['info']}]"
    return f"[{PALETTE['meta']}]◌[/{PALETTE['meta']}]"


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

    action_colors = {
        "deny": PALETTE["deny_bold"],
        "check_in": PALETTE["attention"],
        "proceed": PALETTE["approve"],
        "proceed_flag": PALETTE["info"],
    }
    action_icons = {
        "deny": "×",
        "check_in": "?",
        "proceed": "✓",
        "proceed_flag": "~",
    }

    print()
    print(panel_title("info", f"decision · {stage}"))
    for path in files:
        policy = policies.get(path)
        if policy is None:
            continue
        label = _ACTION_LABELS.get(policy.action, policy.action)
        color = action_colors.get(policy.action, PALETTE["meta"])
        icon = action_icons.get(policy.action, "·")
        dot = _trust_dot(policy)
        print(
            f"  [{color}]{icon}[/{color}] {path}  "
            f"[{color}]{label}[/{color}]  {dot}"
        )


def _show_system_prompt(phase: WorkflowPhase, prompt_text: str) -> None:
    print(f"\n[bold]System prompt ({phase})[/bold]")
    print(prompt_text)


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
