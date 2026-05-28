"""Process-local stash for the apply-stage 'v' (revise scope) flow.

When the developer picks 'v' at the apply check-in, ``_prompt_approval``
returns a deny variant whose feedback string starts with ``[revise]``.
``apply_stage`` writes that feedback into the stash here and exits with
code 0; the REPL outer loop reads the stash and, if non-empty, feeds the
narrow-scope feedback back into the same Hedwig run as a follow-up user
turn — so the visitor sees a smaller patch instead of having to retype
the task. Single-task scope: the stash is cleared after each read.

Process-local by design. The flow is bounded to one task; the stash
should never survive across REPL prompts.
"""

from __future__ import annotations

_PENDING: str | None = None


def stash(feedback: str | None) -> None:
    global _PENDING
    _PENDING = feedback


def take() -> str | None:
    """Return and clear the pending revise feedback, if any."""
    global _PENDING
    value = _PENDING
    _PENDING = None
    return value
