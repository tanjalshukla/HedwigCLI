from __future__ import annotations

"""Developer-facing confirmation for implicit-preference hypotheses.

Visual language matches the soft check-in panel so the two inference-driven
moments in Hedwig's demo (soft check-in and hypothesis confirmation) feel
like the same family.
"""

from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from ..preference_inference import PreferenceHypothesis
from .theme import PALETTE, moment, panel_title

_CONSOLE = Console()


@dataclass(frozen=True)
class HypothesisConfirmation:
    confirmed: bool
    # True if the developer explicitly declined; False if they timed out or
    # dismissed. Distinguished for future "also learn from denials" work.
    explicit_denial: bool = False


def render_hypothesis_confirmation(
    hypothesis: PreferenceHypothesis,
) -> HypothesisConfirmation:
    """Surface a hypothesis with evidence and capture yes/no.

    A brief pause before rendering makes this feel like a deliberate moment
    rather than something that scrolls past. This is the most important UI
    beat in the whole demo.
    """
    style = moment("learn")

    body = Text()
    body.append("\n", style="white")
    body.append(hypothesis.prompt, style="bold white")
    body.append("\n\n", style="white")
    body.append(hypothesis.rationale, style="white")
    body.append("\n", style="white")

    # Extra blank line gives the panel breathing room so it doesn't scroll past.
    _CONSOLE.print()
    _CONSOLE.print()
    _CONSOLE.print(
        Panel(
            body,
            title=panel_title("learn", "I noticed a pattern"),
            border_style=style.border,
            padding=(1, 2),
        )
    )

    _CONSOLE.print(
        f"  [{PALETTE['meta']}](You can review or remove saved preferences with /prefs)[/{PALETTE['meta']}]"
    )
    try:
        response = Prompt.ask(
            f"[{PALETTE['learn_bold']}]Save this as a rule for future sessions?[/{PALETTE['learn_bold']}] (y/n)",
            choices=["y", "n"],
            default="y",
        )
    except (KeyboardInterrupt, EOFError):
        # Ctrl-C or pipe close → treat as dismissed, not a crash.
        _CONSOLE.print(f"[{PALETTE['meta']}](dismissed)[/{PALETTE['meta']}]")
        return HypothesisConfirmation(confirmed=False, explicit_denial=False)

    confirmed = response == "y"
    if confirmed:
        _CONSOLE.print(f"[{PALETTE['learn_bold']}]✦ preference saved[/{PALETTE['learn_bold']}]")
    return HypothesisConfirmation(
        confirmed=confirmed,
        explicit_denial=not confirmed,
    )
