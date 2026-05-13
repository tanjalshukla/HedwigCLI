from __future__ import annotations

"""Non-blocking soft check-in UI.

A soft check-in surfaces what's about to happen but doesn't block execution.
Visual language matches the patch renderer (cyan-accented Rich panels) so
soft check-ins and full check-ins feel like the same family.

Semantics (shipped version):

- A rich panel shows what's being proposed and the Hedwig rationale.
- A short pause window (default 2.5 seconds) gives the developer time to
  intervene by pressing Enter; otherwise execution proceeds.
- If the developer intervenes, the soft check-in escalates to a full
  check-in flow.

Kept deliberately minimal. The UX tuning (exact countdown, keybindings,
panel polish) happens in the UI polish pass.
"""

import select
import sys
import time
from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .theme import PALETTE, moment, panel_title


SOFT_CHECKIN_WINDOW_SECONDS = 2.5


@dataclass(frozen=True)
class SoftCheckinOutcome:
    """Result of a soft check-in panel. If intervened=True the caller should
    escalate to a full check-in. Otherwise execution proceeds."""

    intervened: bool


def render_soft_checkin(
    *,
    stage: str,
    files: list[str],
    reason: str | None,
    window_seconds: float = SOFT_CHECKIN_WINDOW_SECONDS,
) -> SoftCheckinOutcome:
    """Draw the soft check-in panel and poll for developer intervention.

    The panel stays on screen for ``window_seconds``. If the developer presses
    Enter during that window, the check-in escalates. Otherwise the soft
    check-in records an implicit approval and returns intervened=False.
    """
    from rich.live import Live

    console = Console()
    style = moment("soft_checkin")

    def _panel(remaining: float) -> Panel:
        body = Text()
        body.append(f"{stage}\n", style=PALETTE["info_bold"])
        if files:
            body.append("\n")
            for f in files:
                body.append(f"  · {f}\n", style="white")
        if reason:
            body.append("\n")
            body.append("Why: ", style=PALETTE["meta"])
            body.append(reason + "\n", style=PALETTE["meta_italic"])

        # Countdown bar. 20 cells total, fills from full at start and
        # drains as time elapses. Purely visual; doesn't gate input.
        body.append("\n")
        cells = 20
        if window_seconds > 0:
            filled = max(0, int(round((remaining / window_seconds) * cells)))
        else:
            filled = 0
        empty = cells - filled
        body.append("  ")
        body.append("█" * filled, style=PALETTE["attention"])
        body.append("░" * empty, style=PALETTE["meta"])
        body.append(f"  {remaining:>4.1f}s", style=PALETTE["attention"])
        body.append("\n")
        body.append(
            "  press Enter to intervene · otherwise proceeding",
            style=PALETTE["meta"],
        )

        return Panel(
            body,
            title=panel_title("soft_checkin"),
            border_style=style.border,
            padding=(1, 2),
        )

    if not sys.stdin.isatty() or window_seconds <= 0:
        # Non-interactive path — render once and return.
        console.print(_panel(window_seconds))
        intervened = False
    else:
        # Interactive path — animate the bar while polling stdin.
        intervened = False
        end_time = time.monotonic() + window_seconds
        refresh_hz = 10
        with Live(_panel(window_seconds), console=console, refresh_per_second=refresh_hz) as live:
            while True:
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 1.0 / refresh_hz))
                if ready:
                    sys.stdin.readline()
                    intervened = True
                    break
                live.update(_panel(max(remaining, 0)))

    if intervened:
        console.print(
            f"[{PALETTE['attention']}]→ intervention requested · escalating[/{PALETTE['attention']}]"
        )
    else:
        console.print(f"[{PALETTE['meta']}]→ proceeding[/{PALETTE['meta']}]")
    return SoftCheckinOutcome(intervened=intervened)


def _wait_for_enter(window_seconds: float) -> bool:
    """Return True if the developer pressed Enter within window_seconds.

    Uses select() on stdin so we don't block. On non-tty environments
    (tests, pipes), returns False immediately — the soft check-in degrades
    gracefully to "proceed" rather than hanging.
    """
    if not sys.stdin.isatty():
        return False

    end_time = time.monotonic() + window_seconds
    while True:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 0.1))
        if ready:
            # Drain the line so subsequent input isn't polluted.
            sys.stdin.readline()
            return True
