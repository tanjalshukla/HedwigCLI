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


_CONSOLE = Console()

SOFT_CHECKIN_WINDOW_SECONDS = 5.0  # 20s was dead air in a live demo; 5s is enough to read and decide


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

    style = moment("soft_checkin")

    def _panel(remaining: float) -> Panel:
        body = Text()
        stage_label = "About to apply changes to:" if stage == "apply" else f"{stage}"
        body.append(f"{stage_label}\n", style=PALETTE["info_bold"])
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
        body.append("█" * filled, style=PALETTE["attention_bold"])
        body.append("░" * empty, style=PALETTE["info_dim"])
        body.append("\n")
        body.append(
            f"  press Enter to review · continuing in {remaining:.0f}s",
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
        _CONSOLE.print(_panel(window_seconds))
        intervened = False
    else:
        # Interactive path — animate the bar while polling stdin.
        # Ctrl-C during the window is treated as intervention, not a crash.
        intervened = False
        end_time = time.monotonic() + window_seconds
        refresh_hz = 4  # 250ms intervals — bar updates every cell (0.25s), 5s window = 20 frames
        try:
            with Live(_panel(window_seconds), console=_CONSOLE, refresh_per_second=refresh_hz) as live:
                while True:
                    remaining = end_time - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 1.0 / refresh_hz))
                    except (select.error, OSError):
                        # select() not supported on this platform (e.g. Windows stdin)
                        break
                    if ready:
                        sys.stdin.readline()
                        intervened = True
                        break
                    live.update(_panel(max(remaining, 0)))
        except (KeyboardInterrupt, EOFError):
            intervened = True

    if intervened:
        _CONSOLE.print(
            f"[{PALETTE['attention']}]→ stopping for your review[/{PALETTE['attention']}]"
        )
    else:
        _CONSOLE.print(f"[{PALETTE['meta']}]→ proceeding with the change[/{PALETTE['meta']}]")
    return SoftCheckinOutcome(intervened=intervened)
