from __future__ import annotations

"""Natural-language status summaries for Hedwig.

Two tiers:

1. **Facts** — rendered from structured data with deterministic templates.
   Counts, confirmations, which triggers fired. Never wrong.

2. **Prose explanations** — optional LLM-written one-liners that rephrase
   individual facts into human language. Bounded: the LLM only ever
   rephrases a single already-known fact. It never gets raw traces to
   extrapolate from, so it can't invent learnings.

The LLM layer is wrapped in try/except — if Bedrock fails, slow, or is
unreachable, the status command falls back to the deterministic template
sentence. Demo never breaks.
"""

from dataclasses import dataclass

from .preference_inference import SessionSummary, infer_user_persona
# LearnedPreference now lives in repo_memory.py (shared with the plugin).
# Re-exported here so existing importers (commands/status, repl) are unchanged.
from .repo_memory import LearnedPreference  # noqa: F401


# ---------------------------------------------------------------------------
# Structured status data — computed from traces + trust DB, no jargon.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionStatus:
    """Complete status payload. Every field is already humanized — no enum
    strings leak through. Templates and LLM layer both read from this."""

    # Session rhythm, plain English.
    session_style: str      # "engaged back-and-forth" | "delegating" | "just starting"
    turns_so_far: int
    approvals: int
    corrections: int
    denials: int
    failures_reported: int  # developer-reported failures
    proactive_pauses: int   # Hedwig paused without being asked

    # What Hedwig has learned.
    session_preferences: tuple[LearnedPreference, ...]  # scoped to this session
    persistent_preferences: tuple[LearnedPreference, ...]  # across sessions in this repo

    # A one-sentence explanation of the most recent proactive pause, if any.
    most_recent_proactive_reason: str | None

    @property
    def has_learned_anything(self) -> bool:
        return bool(self.session_preferences or self.persistent_preferences)


# ---------------------------------------------------------------------------
# Builders — turn the raw trace/db state into a SessionStatus.
# ---------------------------------------------------------------------------


_STYLE_PHRASES = {
    "active": "engaged back-and-forth",
    "delegating": "mostly delegating",
    "unknown": "just getting started",
}


def build_session_status(
    *,
    summary: SessionSummary,
    confirmed_session_preferences: tuple[LearnedPreference, ...] = (),
    persistent_preferences: tuple[LearnedPreference, ...] = (),
    most_recent_proactive_reason: str | None = None,
) -> SessionStatus:
    """Package a SessionStatus. Caller is responsible for loading the
    confirmed preferences from the trust DB and humanizing them."""
    if summary.n_turns == 0:
        style = "just starting"
    else:
        persona = infer_user_persona(summary).value
        style = _STYLE_PHRASES.get(persona, "just getting started")

    return SessionStatus(
        session_style=style,
        turns_so_far=summary.n_turns,
        approvals=summary.n_approvals,
        corrections=summary.n_feedback,
        denials=summary.n_denials,
        failures_reported=summary.n_failures,
        proactive_pauses=0,  # populated by caller when trace rows are scanned
        session_preferences=confirmed_session_preferences,
        persistent_preferences=persistent_preferences,
        most_recent_proactive_reason=most_recent_proactive_reason,
    )


# ---------------------------------------------------------------------------
# Template renderers — deterministic, demo-safe.
# ---------------------------------------------------------------------------


def template_session_sentence(status: SessionStatus) -> str:
    """One-sentence summary of what's happened this session, in plain English.
    Deterministic — same input always produces the same sentence.
    """
    if status.turns_so_far == 0:
        return "We haven't exchanged anything yet this session."

    # Build the sentence piecewise so grammar stays natural.
    rhythm = status.session_style

    tail_parts: list[str] = []
    if status.corrections > 0:
        tail_parts.append(
            f"you corrected me {status.corrections} time{'s' if status.corrections != 1 else ''}"
        )
    if status.denials > 0:
        tail_parts.append(
            f"denied {status.denials} proposal{'s' if status.denials != 1 else ''}"
        )
    if status.failures_reported > 0:
        tail_parts.append(
            f"reported {status.failures_reported} failure{'s' if status.failures_reported != 1 else ''}"
        )

    if not tail_parts:
        tail = "mostly smoothly"
    else:
        tail = ", ".join(tail_parts)

    turn_word = "turn" if status.turns_so_far == 1 else "turns"
    return (
        f"It's been {rhythm} so far — {status.turns_so_far} {turn_word} in, "
        f"{tail}."
    )


def template_proactive_pause_sentence(status: SessionStatus) -> str | None:
    """Describe the most recent proactive pause, if any."""
    if status.proactive_pauses == 0:
        return None
    reason = status.most_recent_proactive_reason or "a risk signal fired"
    if status.proactive_pauses == 1:
        return f"I paused you proactively once — {reason}."
    return (
        f"I paused you proactively {status.proactive_pauses} times "
        f"(most recently: {reason})."
    )


def template_preference_line(pref: LearnedPreference) -> str:
    """One bullet — headline + basis, plain English."""
    return f"· {pref.headline} — {pref.basis}"


def render_status_text(status: SessionStatus) -> list[str]:
    """Return the complete status as a list of text lines. No Rich markup;
    callers layer colors on top."""
    lines: list[str] = []

    # Session rhythm sentence.
    lines.append(template_session_sentence(status))

    # Proactive pause sentence, if any.
    pause_line = template_proactive_pause_sentence(status)
    if pause_line:
        lines.append(pause_line)

    # Session-scoped preferences.
    if status.session_preferences:
        lines.append("")
        lines.append("What I've picked up from you in this session:")
        for pref in status.session_preferences:
            lines.append(f"  {template_preference_line(pref)}")

    # Persistent repo-scoped preferences.
    if status.persistent_preferences:
        lines.append("")
        lines.append("What I'm carrying from earlier sessions in this repo:")
        for pref in status.persistent_preferences:
            lines.append(f"  {template_preference_line(pref)}")

    if not status.has_learned_anything and status.turns_so_far > 0:
        lines.append("")
        lines.append(
            "I haven't inferred any preferences yet — it usually takes a few "
            "corrections in the same direction before I ask."
        )

    return lines


# ---------------------------------------------------------------------------
# Optional LLM embellishment. Falls back to the template on failure.
# ---------------------------------------------------------------------------


def embellish_preference_basis(
    pref: LearnedPreference,
    *,
    llm_caller=None,
) -> str:
    """Rephrase the *basis* field into more natural prose via LLM. Constrained:
    the LLM only sees the already-known headline and basis — it can't invent.

    If ``llm_caller`` is None (or fails), returns the original basis verbatim.
    """
    if llm_caller is None:
        return pref.basis
    try:
        prompt = (
            "Rewrite this one-sentence 'why' into a single natural, "
            "conversational sentence. Keep the facts exactly the same. "
            "Do not add any new information. Do not hedge. Be direct. "
            "Max 20 words.\n\n"
            f"Headline: {pref.headline}\n"
            f"Why: {pref.basis}\n\n"
            "Rewritten why:"
        )
        result = llm_caller(prompt)
        text = (result or "").strip()
        # Basic guardrails — reject suspiciously long or empty outputs.
        if not text or len(text) > 300:
            return pref.basis
        return text
    except Exception:
        return pref.basis
