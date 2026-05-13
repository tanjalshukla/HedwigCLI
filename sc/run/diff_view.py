from __future__ import annotations

"""Claude-Code-style inline diff rendering for approval prompts.

The apply pipeline still writes via atomic file replacement (sc/patch.py). This
module only renders the proposed diff to the developer for review before
they approve or deny. It does not change where the edits come from or how
they're applied.
"""

import difflib
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from .helpers import _normalize_line_endings
from .theme import PALETTE, moment


_MAX_HUNKS_PER_FILE = 8
_MAX_LINES_PER_HUNK = 40


def _file_label(path: str, is_new: bool) -> str:
    if is_new:
        return f"[{PALETTE['approve_bold']}]± new[/{PALETTE['approve_bold']}]  [bold]{path}[/bold]"
    return f"[{PALETTE['info_bold']}]± edit[/{PALETTE['info_bold']}]  [bold]{path}[/bold]"


def _hunk_to_text(hunk: list[str]) -> Text:
    """Render one unified-diff hunk as colored Rich text. Lines are the raw
    difflib output (including the ``@@`` header, which we color separately)."""
    text = Text()
    line_count = 0
    for raw in hunk:
        if line_count >= _MAX_LINES_PER_HUNK:
            text.append(
                f"  … ({len(hunk) - line_count} more lines)\n",
                style=PALETTE["meta"],
            )
            break
        line = raw.rstrip("\n")
        if line.startswith("@@"):
            text.append(line + "\n", style=PALETTE["info_bold"])
        elif line.startswith("+"):
            text.append(line + "\n", style=PALETTE["approve"])
        elif line.startswith("-"):
            text.append(line + "\n", style=PALETTE["deny"])
        else:
            text.append(line + "\n", style=PALETTE["meta"])
        line_count += 1
    return text


def _split_hunks(diff_lines: list[str]) -> list[list[str]]:
    hunks: list[list[str]] = []
    current: list[str] = []
    for line in diff_lines:
        if line.startswith("@@"):
            if current:
                hunks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        hunks.append(current)
    return hunks


def _render_single_file_diff(
    console: Console,
    path: str,
    old_content: str,
    new_content: str,
    is_new_file: bool,
) -> None:
    old_norm = _normalize_line_endings(old_content)
    new_norm = _normalize_line_endings(new_content)

    diff_style = moment("diff")

    if old_norm == new_norm:
        console.print(
            Panel(
                Text("(no change)", style=PALETTE["meta"]),
                title=_file_label(path, is_new_file),
                border_style=diff_style.border,
                padding=(0, 1),
            )
        )
        return

    if is_new_file:
        preview = new_norm
        if len(preview.splitlines()) > _MAX_LINES_PER_HUNK * 2:
            head = "\n".join(preview.splitlines()[: _MAX_LINES_PER_HUNK * 2])
            preview = head + f"\n… ({len(new_norm.splitlines()) - _MAX_LINES_PER_HUNK * 2} more lines)"
        try:
            body = Syntax(preview, Path(path).suffix.lstrip(".") or "text",
                          theme="ansi_dark", background_color="default")
        except Exception:
            body = Text(preview)
        console.print(
            Panel(
                body,
                title=_file_label(path, True),
                border_style=PALETTE["approve"],
                padding=(0, 1),
            )
        )
        return

    diff_lines = list(
        difflib.unified_diff(
            old_norm.splitlines(keepends=False),
            new_norm.splitlines(keepends=False),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=3,
        )
    )
    # Strip the file-header lines (---/+++); we render our own label.
    hunks = _split_hunks([line for line in diff_lines if not line.startswith(("---", "+++"))])
    if not hunks:
        console.print(
            Panel(
                Text("(whitespace-only change)", style=PALETTE["meta"]),
                title=_file_label(path, False),
                border_style=diff_style.border,
                padding=(0, 1),
            )
        )
        return

    display = hunks[:_MAX_HUNKS_PER_FILE]
    body = Text()
    for i, hunk in enumerate(display):
        body.append_text(_hunk_to_text(hunk))
        if i < len(display) - 1:
            body.append("\n")
    if len(hunks) > _MAX_HUNKS_PER_FILE:
        body.append(
            f"\n… ({len(hunks) - _MAX_HUNKS_PER_FILE} more hunks collapsed)",
            style=PALETTE["meta"],
        )

    console.print(
        Panel(
            body,
            title=_file_label(path, False),
            border_style=diff_style.border,
            padding=(0, 1),
        )
    )


def render_proposed_patch(
    repo_root: Path,
    updates: dict[str, str],
    files: list[str],
) -> None:
    """Pretty-print the proposed patch to the developer before they approve.

    One panel per file. Green +, red -, cyan hunk headers. Large diffs are
    truncated per-hunk and per-file so the review stays readable in a terminal.
    """
    console = Console()
    for path in files:
        new_content = updates.get(path, "")
        file_path = repo_root / path
        is_new_file = not file_path.exists()
        old_content = ""
        if not is_new_file:
            try:
                old_content = file_path.read_text()
            except Exception:
                old_content = ""
        _render_single_file_diff(console, path, old_content, new_content, is_new_file)
