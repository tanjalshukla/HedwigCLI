from __future__ import annotations

"""Single source of truth for Hedwig's visual language.

Every panel, table, and prompt in the runtime imports from here. To tune
the look of the entire CLI, tune this file — no grep needed.

Design principles:
- Balance playful and professional. Cyan for information, magenta reserved
  exclusively for learning moments (the rare ones), green/red for concrete
  approve/deny decisions, yellow for attention-without-alarm.
- Semantic color: never decorative. If a color is there, it means something.
- Iconography is minimal — one glyph per moment category. Never mixed.
- Typography via Rich styles, not ad-hoc hex codes.
"""

from dataclasses import dataclass
from typing import Literal


# ---------------------------------------------------------------------------
# Semantic color palette. Values are Rich style strings — never raw hex here.
# ---------------------------------------------------------------------------


PALETTE = {
    # Information / neutral-positive default
    "info": "cyan",
    "info_dim": "dim cyan",
    "info_bold": "bold cyan",
    # Learning moments — hypotheses, confirmed preferences, growth signals.
    "learn": "green",
    "learn_bold": "bold green",
    # Concrete developer decisions.
    "approve": "bright_green",
    "approve_bold": "bold bright_green",
    "deny": "red",
    "deny_bold": "bold red",
    # Attention without alarm (soft check-in, flagged-for-review, defer).
    "attention": "yellow",
    "attention_bold": "bold yellow",
    # Meta / rationale / secondary text. White (not dim) so it stays legible
    # at booth viewing distance; italic carries the "secondary" feel.
    "meta": "white",
    "meta_italic": "italic white",
    # Success / done — slightly different from approve, used for post-state.
    "done": "bright_green",
}


# ---------------------------------------------------------------------------
# Moment families. Every user-facing moment belongs to exactly one family,
# and every family has a fixed visual signature (color, icon, title prefix).
# ---------------------------------------------------------------------------


MomentFamily = Literal[
    "info",             # policy snapshots, file lists, general output
    "learn",            # hypothesis confirmations, preference inference surfaces
    "approve_request",  # full check-in prompts requesting a decision
    "soft_checkin",     # non-blocking proceeds-unless-intervened
    "failure_signal",   # empirically-grounded proactive check-in
    "scope_aware",      # check-in adapted to scope-constraint pushback
    "failure_aware",    # check-in adapted to failure-report pushback
    "diff",             # the patch being proposed
    "rule_hard",        # hard-constraint compilation output
    "rule_soft",        # behavioral-guideline compilation output
    "observe",          # observability tables and dashboards
]


@dataclass(frozen=True)
class MomentStyle:
    """Visual signature for one moment family."""

    border: str
    title_style: str
    icon: str
    # Short lowercase name shown in panel titles after the icon.
    name: str


MOMENTS: dict[str, MomentStyle] = {
    "info": MomentStyle(
        border="cyan",
        title_style="bold cyan",
        icon="◆",
        name="hedwig",
    ),
    "learn": MomentStyle(
        border="green",
        title_style="bold green",
        icon="✦",
        name="hedwig · learning",
    ),
    "approve_request": MomentStyle(
        border="cyan",
        title_style="bold cyan",
        icon="◉",
        name="hedwig · check-in",
    ),
    "soft_checkin": MomentStyle(
        border="cyan",
        title_style="bold cyan",
        icon="⟳",
        name="hedwig · soft check-in",
    ),
    "failure_signal": MomentStyle(
        border="red",
        title_style="bold red",
        icon="!",
        name="hedwig · failure signal",
    ),
    "scope_aware": MomentStyle(
        border="cyan",
        title_style="bold cyan",
        icon="□",
        name="hedwig · scope-aware",
    ),
    "failure_aware": MomentStyle(
        border="red",
        title_style="bold red",
        icon="!",
        name="hedwig · failure-aware",
    ),
    "diff": MomentStyle(
        border="cyan",
        title_style="bold cyan",
        icon="±",
        name="",  # diffs carry their own per-file labels
    ),
    "rule_hard": MomentStyle(
        # Deep blue for hard constraints — reads as "serious, non-negotiable"
        # without the error connotation red carries. Red stays reserved for
        # genuine failure states.
        border="blue",
        title_style="bold blue",
        icon="■",
        name="hard constraint",
    ),
    "rule_soft": MomentStyle(
        border="yellow",
        title_style="bold yellow",
        icon="▢",
        name="behavioral guideline",
    ),
    "observe": MomentStyle(
        border="cyan",
        title_style="bold cyan",
        icon="◇",
        name="hedwig · observe",
    ),
}


def moment(family: MomentFamily) -> MomentStyle:
    """Convenience accessor. Falls back to 'info' if the family is unknown —
    callers should always pass a valid literal but we don't want to crash
    on a typo during a demo."""
    return MOMENTS.get(family, MOMENTS["info"])


# ---------------------------------------------------------------------------
# Shared rendering primitives. Wraps Rich's Panel / Table / Text with the
# moment-family defaults so callers don't set border-style / title-style /
# padding individually.
# ---------------------------------------------------------------------------


def panel_title(family: MomentFamily, subtitle: str | None = None) -> str:
    """Build the title string for a Rich Panel.

    Shape: "[bold cyan]◆ hedwig[/bold cyan]" or with subtitle
    "[bold cyan]◆ hedwig · <subtitle>[/bold cyan]".
    Keeps a single canonical format so every panel looks related.
    """
    style = moment(family)
    base = style.name
    if subtitle:
        if base:
            base = f"{base} · {subtitle}"
        else:
            base = subtitle
    return f"[{style.title_style}]{style.icon} {base}[/{style.title_style}]"


