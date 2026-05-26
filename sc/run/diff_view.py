from __future__ import annotations

"""Claude-Code-style inline diff rendering for approval prompts.

The apply pipeline still writes via atomic file replacement (see
``_write_updates_atomically`` in ``apply_stage.py``). This module only renders
the proposed diff to the developer for review before they approve or deny. It
does not change where the edits come from or how they're applied.
"""

import difflib
import re
from pathlib import Path

from rich.console import Console
from rich.text import Text

from .helpers import _normalize_line_endings
from .theme import PALETTE


# Cap on lines displayed per file. Hedwig-edited files are typically small;
# anything bigger than this is almost certainly a generated wall.
_MAX_LINES_PER_FILE = 80
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Tracks file paths whose diffs were rendered inline during the streaming
# phase, so the post-hoc batch render can skip them. Consumed (and cleared)
# by ``render_proposed_patch`` once per apply turn.
_STREAMED_THIS_TURN: set[str] = set()


def mark_streamed(path: str) -> None:
    _STREAMED_THIS_TURN.add(path)


def consume_streamed() -> set[str]:
    snapshot = set(_STREAMED_THIS_TURN)
    _STREAMED_THIS_TURN.clear()
    return snapshot


# Subtle row-tinting so the diff reads as a code surface, not raw terminal text.
# Greens / reds for changed rows, a near-black tint for context, dim grey for
# the line-number gutter. Picked to be readable on both light and dark themes.
_ADD_STYLE = f"{PALETTE['approve']} on grey15"
_DEL_STYLE = f"{PALETTE['deny']} on grey15"
_CTX_STYLE = f"{PALETTE['meta']} on grey11"
_GUTTER_STYLE = f"{PALETTE['meta']} on grey11"
_GUTTER_ADD_STYLE = f"{PALETTE['meta']} on grey15"
_GUTTER_DEL_STYLE = f"{PALETTE['meta']} on grey15"


def _file_header(path: str, is_new: bool, added: int, removed: int) -> tuple[Text, Text]:
    """Claude-Code IDE-style header pair: ● Verb(path) / └ Added N, removed M."""
    verb = "Create" if is_new else "Update"
    title = Text()
    title.append("● ", style=PALETTE["approve_bold"])
    title.append(f"{verb}(", style="bold")
    title.append(path, style=f"bold underline {PALETTE['info_bold']}")
    title.append(")", style="bold")

    sub = Text()
    sub.append("  ⎿  ", style=PALETTE["meta"])
    if is_new:
        sub.append("New file, ", style=PALETTE["meta"])
        sub.append(f"{added}", style=f"bold {PALETTE['approve']}")
        sub.append(" lines", style=PALETTE["meta"])
    else:
        sub.append("Added ", style=PALETTE["meta"])
        sub.append(f"{added}", style=f"bold {PALETTE['approve']}")
        sub.append(f" line{'s' if added != 1 else ''}, removed ", style=PALETTE["meta"])
        sub.append(f"{removed}", style=f"bold {PALETTE['deny']}")
        sub.append(f" line{'s' if removed != 1 else ''}", style=PALETTE["meta"])
    return title, sub


def _diff_row(
    console: Console, lineno: int | None, marker: str, body: str,
    body_style: str, gutter_style: str,
) -> Text:
    """Build one diff row: gutter (line-number) + body, both padded to the
    full console width so the background tint reads as a continuous strip."""
    width = max(console.size.width, 60)
    gutter_text = f"  {lineno:>4} " if lineno is not None else "       "
    raw_body = f" {marker} {body}"
    # Truncate overlong lines so they don't wrap and break the tint band.
    max_body = max(width - len(gutter_text) - 1, 20)
    if len(raw_body) > max_body:
        raw_body = raw_body[: max_body - 1] + "…"
    # Pad to fill the row, so the background extends across the terminal.
    padded_body = raw_body + " " * (max_body - len(raw_body))
    row = Text()
    row.append(gutter_text, style=gutter_style)
    row.append(padded_body, style=body_style)
    return row


def _render_unified_diff(
    console: Console,
    path: str,
    old_content: str,
    new_content: str,
    is_new_file: bool,
) -> None:
    """Render a Claude-Code-style inline diff: dim header, line numbers in the
    margin, green +, red -, no panels, no @@ markers."""
    old_norm = _normalize_line_endings(old_content)
    new_norm = _normalize_line_endings(new_content)

    if old_norm == new_norm and not is_new_file:
        console.print()
        title, _ = _file_header(path, False, 0, 0)
        console.print(title)
        console.print(Text("  ⎿  (no change)", style=PALETTE["meta"]))
        return

    if is_new_file:
        new_lines = new_norm.splitlines()
        added = len(new_lines)
        console.print()
        title, sub = _file_header(path, True, added, 0)
        console.print(title)
        console.print(sub)
        shown = new_lines[:_MAX_LINES_PER_FILE]
        for i, line in enumerate(shown, start=1):
            console.print(_diff_row(console, i, "+", line, _ADD_STYLE, _GUTTER_ADD_STYLE))
        if added > _MAX_LINES_PER_FILE:
            console.print(
                Text(f"      … {added - _MAX_LINES_PER_FILE} more lines",
                     style=PALETTE["meta"])
            )
        return

    diff_lines = list(
        difflib.unified_diff(
            old_norm.splitlines(keepends=False),
            new_norm.splitlines(keepends=False),
            n=3,
            lineterm="",
        )
    )
    diff_lines = [ln for ln in diff_lines if not ln.startswith(("---", "+++"))]

    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("@@"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("@@"))

    console.print()
    title, sub = _file_header(path, False, added, removed)
    console.print(title)
    console.print(sub)

    if not diff_lines:
        console.print(Text("      (whitespace-only change)", style=PALETTE["meta"]))
        return

    new_lineno = 0
    old_lineno = 0
    rendered = 0
    last_was_skip = False
    first_hunk = True
    for raw in diff_lines:
        if rendered >= _MAX_LINES_PER_FILE:
            console.print(
                Text(f"      … {len(diff_lines) - rendered} more lines",
                     style=PALETTE["meta"])
            )
            break

        m = _HUNK_HEADER_RE.match(raw)
        if m:
            old_lineno = int(m.group(1))
            new_lineno = int(m.group(2))
            if not first_hunk and not last_was_skip:
                console.print(Text("      ⋮", style=PALETTE["meta"]))
                last_was_skip = True
            first_hunk = False
            continue

        last_was_skip = False
        if raw.startswith("+"):
            console.print(_diff_row(console, new_lineno, "+", raw[1:], _ADD_STYLE, _GUTTER_ADD_STYLE))
            new_lineno += 1
        elif raw.startswith("-"):
            console.print(_diff_row(console, old_lineno, "-", raw[1:], _DEL_STYLE, _GUTTER_DEL_STYLE))
            old_lineno += 1
        else:
            body = raw[1:] if raw.startswith(" ") else raw
            console.print(_diff_row(console, new_lineno, " ", body, _CTX_STYLE, _GUTTER_STYLE))
            new_lineno += 1
            old_lineno += 1
        rendered += 1


# Backwards-compat alias for callers (apply_ui imports the old name).
def _render_single_file_diff(
    console: Console,
    path: str,
    old_content: str,
    new_content: str,
    is_new_file: bool,
) -> None:
    _render_unified_diff(console, path, old_content, new_content, is_new_file)


def render_proposed_patch(
    repo_root: Path,
    updates: dict[str, str],
    files: list[str],
) -> None:
    """Pretty-print the proposed patch to the developer before they approve.

    One panel per file. Green +, red -, cyan hunk headers. Large diffs are
    truncated per-hunk and per-file so the review stays readable in a terminal.
    """
    already = consume_streamed()
    console = Console()
    for path in files:
        if path in already:
            # Already rendered inline as it streamed in.
            continue
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
