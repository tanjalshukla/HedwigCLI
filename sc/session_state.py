from __future__ import annotations

"""Session-state signals — per-turn observations about the *shape* of the
current task that go beyond per-action risk.

Where ``features.RiskSignals`` answers "is this one action risky?",
``SessionState`` answers "is the agent thrashing?", "has scope drifted from
the declared intent?", "have verifications been failing within this turn?"
These are the signals that make the protocol mode-agnostic: tiny in a
one-shot, dominant in a long autonomous loop, computed identically either
way.

Pure functions over ``decision_traces`` rows. No SQL here — callers pass in
the rows they already have (typically from
``trust_db.session_traces(repo_root, session_id)``). Day 3 plumbs these
into the cascade via the protocol layer; Day 1 only exposes the
computation and tests it.

Three signals, ordered by load-bearing-ness:

* ``file_touch_counts`` — how many times each file has been governed this
  task. Repeated touches of the same file across a single task is the
  thrashing signature.
* ``scope_drift_score`` — Jaccard-style overlap between files touched and
  files implied by the declared intent. Rises when the agent strays from
  what it said it was going to do. (Day 3 gets a richer embedding-based
  variant; this is the keyword baseline.)
* ``intra_turn_verification_failures`` — how many traces this task
  reported verification_passed=0. Direct, unambiguous "something is going
  wrong right now" signal.
"""

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .store.types import DecisionTraceRow


# How many times the same file may be governed within one task before we
# call it thrashing. Threshold is a documented prior, not tuned.
THRASHING_TOUCH_COUNT: int = 3


@dataclass(frozen=True)
class SessionState:
    """Per-task summary of in-flight session shape.

    Always computed for one (session_id, task) pair. ``empty`` when no
    traces match — the cascade then treats every session-state signal as
    no-op, preserving backwards compatibility with code that doesn't yet
    consult session state.
    """

    session_id: str
    task: str
    n_traces: int
    file_touch_counts: Mapping[str, int] = field(default_factory=dict)
    scope_drift_score: float = 0.0  # 0.0 = on-scope; 1.0 = entirely off-scope
    intra_turn_verification_failures: int = 0
    distinct_files_touched: int = 0

    @property
    def empty(self) -> bool:
        return self.n_traces == 0

    @property
    def is_thrashing(self) -> bool:
        """Any single file touched more than the threshold within this task."""
        return any(c >= THRASHING_TOUCH_COUNT for c in self.file_touch_counts.values())

    @property
    def thrashing_files(self) -> tuple[str, ...]:
        """Files crossing the thrashing threshold, in descending touch count."""
        return tuple(
            f for f, c in sorted(
                self.file_touch_counts.items(), key=lambda kv: -kv[1]
            )
            if c >= THRASHING_TOUCH_COUNT
        )


def compute_session_state(
    rows: Iterable[DecisionTraceRow | Mapping[str, object]],
    *,
    session_id: str,
    task: str,
    declared_intent_text: str | None = None,
) -> SessionState:
    """Build a SessionState from in-session trace rows.

    ``rows`` should be the traces for the current ``session_id`` (the caller
    typically has them already from ``trust_db.session_traces``). We filter
    to the current ``task`` here — a session may carry multiple tasks, but
    thrashing and scope drift are *per-task* properties.

    ``declared_intent_text`` is the agent's stated plan for this task (e.g.
    the ``IntentDeclaration.task_summary``). When present, scope drift is
    computed against it. When absent, drift is 0.0.
    """
    matching = [r for r in rows if (r.get("task") or "") == task]
    if not matching:
        return SessionState(session_id=session_id, task=task, n_traces=0)

    file_touches: Counter[str] = Counter()
    intra_turn_failures = 0

    for row in matching:
        path = (row.get("file_path") or "").strip()
        if path and path != "__session__":
            file_touches[path] += 1
        verification_passed = row.get("verification_passed")
        if verification_passed is not None and not bool(verification_passed):
            intra_turn_failures += 1

    drift = _scope_drift(
        declared_intent_text=declared_intent_text,
        files_touched=tuple(file_touches.keys()),
    )

    return SessionState(
        session_id=session_id,
        task=task,
        n_traces=len(matching),
        file_touch_counts=dict(file_touches),
        scope_drift_score=drift,
        intra_turn_verification_failures=intra_turn_failures,
        distinct_files_touched=len(file_touches),
    )


# ---------------------------------------------------------------------------
# Scope drift — keyword baseline.
# ---------------------------------------------------------------------------


def _scope_drift(
    *,
    declared_intent_text: str | None,
    files_touched: tuple[str, ...],
) -> float:
    """Crude keyword-overlap drift score in [0.0, 1.0].

    0.0 = every touched file's basename appears in the declared intent text
          (or no declared intent — we cannot detect drift without a reference).
    1.0 = no touched file's basename appears anywhere in the declared intent.

    This is intentionally simple. A token-level baseline is the right
    complexity for Day 1: we want a signal that's clearly weaker than what
    the embedding-based detector (Day 4) will provide, so the upgrade
    visibly helps. Premature optimization here would muddy that comparison.
    """
    if not declared_intent_text or not files_touched:
        return 0.0

    intent_lower = declared_intent_text.lower()
    on_scope = 0
    for path in files_touched:
        # Match either the basename without extension or the full repo-relative
        # path. Either occurring in the declared intent counts as on-scope.
        basename = path.rsplit("/", 1)[-1]
        stem = basename.rsplit(".", 1)[0]
        if stem and (stem in intent_lower or path.lower() in intent_lower):
            on_scope += 1

    return 1.0 - (on_scope / len(files_touched))
