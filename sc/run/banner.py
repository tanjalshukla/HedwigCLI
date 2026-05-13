from __future__ import annotations

"""Hedwig's identity banner — an owl in ASCII plus a single line of
session context. Shown at `hw init` and at the start of each `hw run`.

Goals:
- Tiny (fits in 6 lines or fewer).
- Playful without being cartoonish.
- Serves as a visual anchor — when the demo audience sees the owl, they
  know Hedwig is taking a turn.
"""

from rich.console import Console

from ..preference_inference import SessionSummary
from .theme import PALETTE


# Compact owl. Four lines, narrow. Sized so it sits comfortably next to
# a single metadata line without dominating the terminal.
OWL_LINES = [
    r"   ,___,   ",
    r"   (O,O)   ",
    r"   (   )   ",
    r"   -\"-\"-  ",
]


def render_banner(
    *,
    mode: str | None = None,
    intensity: str | None = None,
    pinned_intensity: str | None = None,
    model_short: str | None = None,
    session_turn_count: int = 0,
    confirmed_pref_count: int = 0,
) -> None:
    """Render the Hedwig owl plus session context to the right."""
    console = Console()

    right_lines: list[str] = []
    right_lines.append(f"[{PALETTE['info_bold']}]hedwig[/{PALETTE['info_bold']}]")
    if model_short:
        right_lines.append(f"[{PALETTE['meta']}]{model_short}[/{PALETTE['meta']}]")
    if mode:
        right_lines.append(
            f"[{PALETTE['meta']}]mode:[/{PALETTE['meta']}] "
            f"[{PALETTE['info']}]{mode}[/{PALETTE['info']}]"
        )

    # Show intensity — pinned takes precedence over inferred, marked with ⊙
    effective = pinned_intensity or intensity
    if effective and effective != "unknown":
        from .intensity_toggle import _label_from_intensity
        # Translate internal intensity value to user-facing oversight label.
        label = _label_from_intensity(effective) if effective in (None, "active", "delegating") else effective
        color = PALETTE["learn"] if effective == "active" else PALETTE["info"]
        pin_marker = f"[{PALETTE['meta']}] ⊙[/{PALETTE['meta']}]" if pinned_intensity else ""
        right_lines.append(
            f"[{PALETTE['meta']}]oversight:[/{PALETTE['meta']}] "
            f"[{color}]{label}[/{color}]{pin_marker}"
        )

    if confirmed_pref_count > 0:
        right_lines.append(
            f"[{PALETTE['learn']}]✦ {confirmed_pref_count} preference"
            f"{'s' if confirmed_pref_count != 1 else ''} active[/{PALETTE['learn']}]"
        )
    elif session_turn_count > 0:
        right_lines.append(
            f"[{PALETTE['meta']}]prior turns:[/{PALETTE['meta']}] "
            f"[{PALETTE['info']}]{session_turn_count}[/{PALETTE['info']}]"
        )

    while len(right_lines) < len(OWL_LINES):
        right_lines.append("")

    for owl_line, right in zip(OWL_LINES, right_lines):
        console.print(
            f"[{PALETTE['info']}]{owl_line}[/{PALETTE['info']}]  {right}"
        )
    console.print()


def render_session_start_banner(
    *,
    config_model_id: str,
    profile_mode: str,
    prior_session_summary: SessionSummary | None,
    pinned_intensity: str | None = None,
    confirmed_pref_count: int = 0,
) -> None:
    """Session-start variant — shows mode, inferred/pinned intensity, and
    confirmed preference count so the learning story lands immediately."""
    model_short = config_model_id.split("/")[-1] if "/" in config_model_id else config_model_id
    if len(model_short) > 40:
        model_short = model_short[:37] + "..."

    intensity = None
    turns = 0
    if prior_session_summary is not None and prior_session_summary.n_turns > 0:
        from ..preference_inference import infer_user_persona
        intensity = infer_user_persona(prior_session_summary).value
        turns = prior_session_summary.n_turns

    render_banner(
        mode=profile_mode,
        intensity=intensity,
        pinned_intensity=pinned_intensity,
        model_short=model_short,
        session_turn_count=turns,
        confirmed_pref_count=confirmed_pref_count,
    )
