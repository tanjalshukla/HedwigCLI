from __future__ import annotations

"""Check-in style routing — adapt what a check-in panel surfaces based on
the developer's recent pushback pattern.

Two rules for now (demo-scoped):

- Trailing pushback dominated by failure_report → surface any recent
  verification-failure hotspots prominently so the developer's first instinct
  ("is this broken again?") is pre-answered.
- Trailing pushback dominated by scope_constraint → surface the action's
  scope (file count, blast radius) prominently so the developer can narrow
  it without asking.

Visual language matches the soft check-in and hypothesis panels. Called from
apply_stage just before _prompt_approval fires.
"""

from collections import Counter

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..preferences import PushbackType
from .theme import PALETTE, moment, panel_title


_TRAILING_WINDOW = 10  # traces to consider for dominance calculation
_DOMINANCE_RATIO = 0.5  # >=50% of trailing window must share a type


def dominant_pushback_type(trailing_pushback_types: list[str]) -> PushbackType | None:
    """Return the PushbackType that dominates the trailing window, or None
    if no single category is above the dominance threshold."""
    window = trailing_pushback_types[-_TRAILING_WINDOW:]
    if len(window) < 3:
        return None
    counter = Counter(window)
    most_common, count = counter.most_common(1)[0]
    if count / len(window) < _DOMINANCE_RATIO:
        return None
    try:
        return PushbackType(most_common)
    except ValueError:
        return None


def render_adapted_checkin_context(
    *,
    dominant_type: PushbackType | None,
    files: list[str],
    blast_radius: int | None = None,
    recent_verification_failures: list[str] | None = None,
) -> None:
    """Surface extra context above the approval prompt based on pushback
    pattern. No-op if no pattern dominates or no routing rule applies.
    """
    if dominant_type is None:
        return

    console = Console()
    body = Text()

    if dominant_type == PushbackType.FAILURE_REPORT:
        family = "failure_aware"
        body.append(
            "Recent turns have been failure reports. Context before you decide:\n\n",
            style=PALETTE["meta"],
        )
        if recent_verification_failures:
            body.append("Recent verification failures:\n", style=PALETTE["deny_bold"])
            for f in recent_verification_failures[:5]:
                body.append(f"  • {f}\n", style=PALETTE["deny"])
        else:
            body.append(
                "(no recent verification output available)\n",
                style=PALETTE["meta_italic"],
            )

    elif dominant_type == PushbackType.SCOPE_CONSTRAINT:
        family = "scope_aware"
        body.append(
            "You've narrowed scope repeatedly. This action's scope:\n\n",
            style=PALETTE["meta"],
        )
        body.append(f"Files touched: {len(files)}\n", style=PALETTE["info_bold"])
        if blast_radius is not None:
            body.append(f"Blast radius: {blast_radius}\n", style=PALETTE["info_bold"])
        if files:
            body.append("\n")
            for f in files[:10]:
                body.append(f"  • {f}\n", style="white")
            if len(files) > 10:
                body.append(f"  … ({len(files) - 10} more)\n", style=PALETTE["meta"])

    else:
        return  # no routing rule for this pushback type yet

    style = moment(family)  # type: ignore[arg-type]
    console.print(
        Panel(
            body,
            title=panel_title(family),  # type: ignore[arg-type]
            border_style=style.border,
            padding=(1, 2),
        )
    )
