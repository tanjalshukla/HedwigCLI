from __future__ import annotations

"""Run-finalization helpers: summaries and guideline suggestion prompts."""

from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from ..trust_db import TrustDB
from .theme import PALETTE, panel_title
from .ui import _render_file_list


def _render_run_summary(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
) -> None:
    rows = trust_db.session_traces(repo_root, session_id)
    if not rows:
        return
    policy_checkins = sum(
        1 for row in rows
        if str(row["check_in_initiator"] or "") == "policy"
    )
    model_checkins = sum(
        1 for row in rows
        if str(row["check_in_initiator"] or "") == "model_proactive"
    )
    apply_files = sorted(
        {
            str(row["file_path"])
            for row in rows
            if row["stage"] == "apply" and str(row["file_path"]) != "__session__"
        }
    )

    console = Console()
    body = Text()

    # Opening sentence — plain language summary.
    if apply_files:
        n = len(apply_files)
        body.append(
            f"Done — {n} file{'s' if n != 1 else ''} updated.\n\n",
            style=PALETTE["approve_bold"],
        )
        for f in apply_files:
            body.append(f"  · {f}\n", style="white")
        body.append("\n")
    else:
        body.append(
            "Done — no files changed.\n\n",
            style="white",
        )

    # Check-in sentence. Translate "initiator" jargon.
    if policy_checkins or model_checkins:
        checkin_lines: list[str] = []
        if policy_checkins:
            checkin_lines.append(
                f"I paused you {policy_checkins} time{'s' if policy_checkins != 1 else ''}"
            )
        if model_checkins:
            checkin_lines.append(
                f"the model paused {model_checkins} time{'s' if model_checkins != 1 else ''}"
            )
        body.append(" · ".join(checkin_lines) + ".\n", style=PALETTE["info"])
    else:
        body.append(
            "No pauses — the run went smoothly.\n",
            style=PALETTE["meta"],
        )

    # Learning footer — plain language.
    sample_count = trust_db.policy_model_sample_count(repo_root)
    body.append("\n")
    if sample_count >= 10:
        body.append(
            f"Hedwig's learned scorer is active ({sample_count} decisions "
            f"incorporated across this repo).",
            style=PALETTE["learn"],
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
            title=panel_title("info", "run complete"),
            border_style=PALETTE["approve"],
            padding=(1, 2),
        )
    )
    print(
        f"[{PALETTE['meta']}]Use[/{PALETTE['meta']}] hw status "
        f"[{PALETTE['meta']}]to see what Hedwig thinks about this session, "
        f"or[/{PALETTE['meta']}] hw observe export --html "
        f"[{PALETTE['meta']}]for a full report.[/{PALETTE['meta']}]"
    )


def _maybe_prompt_guideline_suggestions(
    *,
    trust_db: TrustDB,
    repo_root: str,
    min_count: int = 3,
) -> None:
    candidates = trust_db.guideline_candidates(repo_root, min_count=min_count, max_items=4)
    if not candidates:
        return
    print("\n[bold]Guideline suggestions from repeated feedback[/bold]")
    selected: list[str] = []
    for item in candidates:
        print(f"- ({item.count}x) {item.guideline}")
        choice = Prompt.ask(
            "Apply (a), edit then apply (e), or skip (s)?",
            choices=["a", "e", "s"],
            default="s",
        )
        if choice == "a":
            selected.append(item.guideline)
        elif choice == "e":
            edited = Prompt.ask("Edited guideline", default=item.guideline).strip()
            if edited:
                selected.append(edited)
    if not selected:
        return
    inserted = trust_db.add_behavioral_guidelines(
        repo_root,
        source="feedback_auto",
        guidelines=selected,
    )
    if inserted:
        print(f"[green]Added {inserted} behavioral guideline(s).[/green]")


def _finalize_run(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
) -> None:
    _render_run_summary(
        trust_db=trust_db,
        repo_root=repo_root,
        session_id=session_id,
    )
    _maybe_prompt_guideline_suggestions(
        trust_db=trust_db,
        repo_root=repo_root,
    )
