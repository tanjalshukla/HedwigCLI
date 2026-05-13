from __future__ import annotations

"""Inline thought-stream for `hw run`.

Replaces the previous rotating-phrase spinner with a live reasoning stream.
Each "thought" is a single short sentence describing what Hedwig is
currently doing or considering — reading a file, checking a constraint,
weighing the scorer output. Emitted one at a time; each thought replaces
the previous line. Claude-Code-inspired.

Usage:

    with ThoughtStream() as ts:
        ts.think("reading task_api/api.py")
        ts.think("no hard constraints match this path")
        ts.think("scorer says 0.63 — borderline, will ask")
        # ... actual work happens ...

The stream is transient — when the context exits, the stream clears and
the next panel renders cleanly.
"""

import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from .theme import PALETTE


# Minimum time each thought stays on screen so the reader can actually see
# it. Even if the caller emits three thoughts in rapid succession, each one
# pauses briefly before the next replaces it.
_MIN_THOUGHT_DWELL_SECONDS = 0.35


class ThoughtStream:
    """A live-updating one-line stream of what Hedwig is doing.

    Caller pushes thoughts via ``think()``. The stream renders each thought
    with a small spinner dot and brand prefix, ensures a minimum dwell time
    so the reader can catch it, and clears cleanly on exit.
    """

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()
        self._live: Live | None = None
        self._spinner = Spinner("dots", style=PALETTE["info"])
        self._lock = threading.Lock()
        self._last_shown_at: float = 0.0
        self._current_text: str = "thinking"

    def __enter__(self) -> "ThoughtStream":
        if not self._console.is_terminal:
            return self
        self._live = Live(
            self._build_renderable(self._current_text),
            console=self._console,
            refresh_per_second=12,
            transient=True,
        )
        self._live.__enter__()
        self._last_shown_at = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live is not None:
            self._live.__exit__(exc_type, exc, tb)
            self._live = None

    def think(self, thought: str) -> None:
        """Push a new thought. Enforces minimum dwell so previous thoughts
        don't flicker past unreadably. Safe to call from any thread."""
        with self._lock:
            if self._live is None:
                # Non-tty path — just print the thought dim so it shows up in
                # captured output (tests, piped output). No rewriting.
                self._console.print(
                    f"[{PALETTE['meta_italic']}]· {thought}[/{PALETTE['meta_italic']}]"
                )
                return

            elapsed = time.monotonic() - self._last_shown_at
            remaining = _MIN_THOUGHT_DWELL_SECONDS - elapsed
            if remaining > 0 and self._current_text != "thinking":
                time.sleep(remaining)

            self._current_text = thought
            self._live.update(self._build_renderable(thought))
            self._last_shown_at = time.monotonic()

    def _build_renderable(self, thought: str) -> Text:
        text = Text()
        text.append("hedwig  ", style=PALETTE["info_bold"])
        text.append(thought, style=PALETTE["meta_italic"])
        return text


@contextmanager
def think_about(initial: str, console: Console | None = None) -> Iterator[ThoughtStream]:
    """Convenience wrapper. Starts a ThoughtStream with an initial thought
    already rendered, so callers don't have to call both __enter__ and
    immediately think()."""
    with ThoughtStream(console=console) as ts:
        ts.think(initial)
        yield ts
