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


@dataclass(frozen=True)
class HypothesisConfirmation:
    confirmed: bool
    # True if the developer explicitly declined; False if they timed out or
    # dismissed. Distinguished for future "also learn from denials" work.
    explicit_denial: bool = False


def render_hypothesis_confirmation(
    hypothesis: PreferenceHypothesis,
) -> HypothesisConfirmation:
    """Surface a single implicit-preference hypothesis and capture the
    developer's yes/no. Blocking by design — this is a small, rare moment
    and we want the developer's attention for it."""
    console = Console()
    style = moment("learn")

    body = Text()
    body.append("I noticed a pattern.\n\n", style=PALETTE["learn_bold"])
    body.append(hypothesis.prompt + "\n\n", style="white")
    body.append(hypothesis.rationale + "\n", style=PALETTE["meta_italic"])

    console.print(
        Panel(
            body,
            title=panel_title("learn", "pattern detected"),
            border_style=style.border,
            padding=(1, 2),
        )
    )

    response = Prompt.ask(
        f"[{PALETTE['learn_bold']}]Accept for the rest of this session?[/{PALETTE['learn_bold']}] (y/n)",
        choices=["y", "n"],
        default="n",
    )
    confirmed = response == "y"
    return HypothesisConfirmation(
        confirmed=confirmed,
        explicit_denial=not confirmed,
    )
