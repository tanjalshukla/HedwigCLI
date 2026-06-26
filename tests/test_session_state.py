"""Day 1 session-state signal tests.

The compute_session_state function is pure, so we exercise it with
hand-rolled trace dicts shaped like decision_traces rows. No SQLite, no
fixtures.
"""

from __future__ import annotations

from sc.session_state import (
    THRASHING_TOUCH_COUNT,
    SessionState,
    compute_session_state,
)


def _trace(
    *,
    task: str = "add pagination to list_tasks",
    file_path: str = "service.py",
    verification_passed: int | None = None,
    user_decision: str = "approve",
) -> dict[str, object]:
    return {
        "task": task,
        "file_path": file_path,
        "verification_passed": verification_passed,
        "user_decision": user_decision,
    }


def test_empty_when_no_traces() -> None:
    state = compute_session_state([], session_id="s1", task="t1")
    assert state.empty
    assert not state.is_thrashing
    assert state.thrashing_files == ()
    assert state.scope_drift_score == 0.0


def test_filters_to_current_task() -> None:
    rows = [
        _trace(task="task A", file_path="foo.py"),
        _trace(task="task B", file_path="bar.py"),
        _trace(task="task A", file_path="foo.py"),
    ]
    state = compute_session_state(rows, session_id="s1", task="task A")
    assert state.n_traces == 2
    assert state.file_touch_counts == {"foo.py": 2}
    assert "bar.py" not in state.file_touch_counts


def test_thrashing_detected_at_threshold() -> None:
    rows = [_trace(file_path="auth.py") for _ in range(THRASHING_TOUCH_COUNT)]
    state = compute_session_state(
        rows, session_id="s1", task="add pagination to list_tasks"
    )
    assert state.is_thrashing
    assert state.thrashing_files == ("auth.py",)


def test_thrashing_not_triggered_below_threshold() -> None:
    rows = [_trace(file_path="auth.py") for _ in range(THRASHING_TOUCH_COUNT - 1)]
    state = compute_session_state(
        rows, session_id="s1", task="add pagination to list_tasks"
    )
    assert not state.is_thrashing


def test_thrashing_files_sorted_by_touch_count() -> None:
    rows = (
        [_trace(file_path="hot.py")] * 5
        + [_trace(file_path="warm.py")] * 4
        + [_trace(file_path="cold.py")] * 1
    )
    state = compute_session_state(
        rows, session_id="s1", task="add pagination to list_tasks"
    )
    # Both hot.py and warm.py exceed the threshold; cold.py does not.
    assert state.thrashing_files == ("hot.py", "warm.py")


def test_intra_turn_verification_failures_counted() -> None:
    rows = [
        _trace(file_path="a.py", verification_passed=1),
        _trace(file_path="b.py", verification_passed=0),
        _trace(file_path="c.py", verification_passed=0),
        _trace(file_path="d.py", verification_passed=None),
    ]
    state = compute_session_state(
        rows, session_id="s1", task="add pagination to list_tasks"
    )
    assert state.intra_turn_verification_failures == 2


def test_session_marker_file_excluded() -> None:
    """The trace store uses '__session__' for synthetic per-session rows;
    they shouldn't count toward file-touch totals."""
    rows = [
        _trace(file_path="__session__"),
        _trace(file_path="real.py"),
    ]
    state = compute_session_state(
        rows, session_id="s1", task="add pagination to list_tasks"
    )
    assert state.distinct_files_touched == 1
    assert "real.py" in state.file_touch_counts
    assert "__session__" not in state.file_touch_counts


def test_scope_drift_zero_when_files_match_declared_intent() -> None:
    rows = [
        _trace(file_path="sc/service.py"),
        _trace(file_path="sc/router.py"),
    ]
    state = compute_session_state(
        rows,
        session_id="s1",
        task="add pagination",
        declared_intent_text="add pagination to service and router",
    )
    assert state.scope_drift_score == 0.0


def test_scope_drift_one_when_no_overlap() -> None:
    rows = [
        _trace(task="add pagination", file_path="payments.py"),
        _trace(task="add pagination", file_path="billing.py"),
    ]
    state = compute_session_state(
        rows,
        session_id="s1",
        task="add pagination",
        declared_intent_text="add pagination to service and router",
    )
    assert state.scope_drift_score == 1.0


def test_scope_drift_zero_without_declared_intent() -> None:
    """Without a reference intent, we can't detect drift — return 0.0
    rather than guess."""
    rows = [_trace(file_path="anywhere.py")]
    state = compute_session_state(rows, session_id="s1", task="x")
    assert state.scope_drift_score == 0.0


def test_session_state_is_frozen() -> None:
    state = SessionState(session_id="s1", task="t1", n_traces=0)
    try:
        state.session_id = "mutated"  # type: ignore[misc]
    except (AttributeError, Exception):
        pass
    else:
        raise AssertionError("SessionState should be immutable")
