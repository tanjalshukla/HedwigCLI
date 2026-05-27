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

_CONSOLE = Console()


# Compact owl. Four lines, narrow. Sized so it sits comfortably next to
# a single metadata line without dominating the terminal.
OWL_LINES = [
    r"   ,___,   ",
    r"   (O,O)   ",
    r"   (   )   ",
    r"   -\"-\"-  ",
]


# Stage → palette key. Every value must be a key already in PALETTE
# (theme.py is the single source of truth — no new colors here).
# Mapping rationale:
#   read    → info        (cyan; the default information family)
#   plan    → meta        (white/secondary; planning is reflective, not active)
#   apply   → learn_bold  (the headline action; the closest harmonious
#                         accent we have to the poster's magenta/pink)
#   verify  → approve_bold (bright green; verification is a confirm moment)
#   report  → meta        (post-action wrap-up, secondary register)
STAGE_PALETTE_KEYS: dict[str, str] = {
    "read": "info",
    "plan": "meta",
    "apply": "learn_bold",
    "verify": "approve_bold",
    "report": "meta",
}


def _banner_word_style(stage: str | None) -> str:
    """Return the Rich style string for the 'hedwig' word and the owl glyph
    given a stage. Falls back to ``info_bold`` (legacy behavior) when no
    stage is supplied or the stage is unknown."""
    if stage is None:
        return PALETTE["info_bold"]
    key = STAGE_PALETTE_KEYS.get(stage, "info_bold")
    return PALETTE[key]


def render_banner(
    *,
    mode: str | None = None,
    intensity: str | None = None,
    pinned_intensity: str | None = None,
    model_short: str | None = None,
    session_turn_count: int = 0,
    confirmed_pref_count: int = 0,
    stage: str | None = None,
) -> None:
    """Render the Hedwig owl plus session context to the right.

    ``stage`` (one of ``read``/``plan``/``apply``/``verify``/``report``)
    shifts the color of the owl and the ``hedwig`` word; default of
    ``None`` keeps the legacy cyan styling so existing call sites are
    unaffected.
    """
    console = _CONSOLE
    word_style = _banner_word_style(stage)

    right_lines: list[str] = []
    right_lines.append(f"[{word_style}]hedwig[/{word_style}]")
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
        from .oversight_toggle import _label_from_intensity
        label = _label_from_intensity(effective)
        color = PALETTE["learn"] if effective == "active" else PALETTE["info"]
        pin_marker = f"[{PALETTE['meta']}] ⊙[/{PALETTE['meta']}]" if pinned_intensity else ""
        right_lines.append(
            f"[{PALETTE['meta']}]oversight:[/{PALETTE['meta']}] "
            f"[{color}]{label}[/{color}]{pin_marker}"
        )

    if confirmed_pref_count > 0:
        pref_label = f"{'s' if confirmed_pref_count != 1 else ''}"
        right_lines.append(
            f"[{PALETTE['learn']}]✦ {confirmed_pref_count} preference{pref_label} active[/{PALETTE['learn']}]"
        )
    if session_turn_count > 0:
        right_lines.append(
            f"[{PALETTE['meta']}]{session_turn_count} turns in this repo[/{PALETTE['meta']}]"
        )

    # Cold-start: when there's no prior history and no confirmed prefs, add a
    # brief note so the empty right-side doesn't look like a rendering glitch.
    if session_turn_count == 0 and confirmed_pref_count == 0:
        right_lines.append(
            "[dim]no prior sessions in this repo · starting fresh[/dim]"
        )

    while len(right_lines) < len(OWL_LINES):
        right_lines.append("")

    # Owl glyph color tracks the same stage shift so the visual is unified.
    # When no stage is passed, fall back to the legacy ``info`` cyan.
    owl_style = word_style if stage is not None else PALETTE["info"]
    for owl_line, right in zip(OWL_LINES, right_lines):
        console.print(
            f"[{owl_style}]{owl_line}[/{owl_style}]  {right}"
        )
    console.print()


def render_session_start_banner(
    *,
    config_model_id: str,
    profile_mode: str,
    prior_session_summary: SessionSummary | None,
    pinned_intensity: str | None = None,
    confirmed_pref_count: int = 0,
    stage: str | None = None,
) -> None:
    """Session-start variant — shows mode, inferred/pinned intensity, and
    confirmed preference count so the learning story lands immediately."""
    model_short = config_model_id.split("/")[-1] if "/" in config_model_id else config_model_id
    # Strip "global.anthropic." prefix from Bedrock inference profile names
    if model_short.startswith("global.anthropic."):
        model_short = model_short[len("global.anthropic."):]
    if len(model_short) > 30:
        model_short = model_short[:27] + "..."

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
        stage=stage,
    )
