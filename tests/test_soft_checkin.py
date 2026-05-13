from __future__ import annotations

import unittest

from sc.preferences import (
    force_action_from_preferences,
    Condition,
    Lifecycle,
    Preference,
    PreferenceAction,
    Scope,
    Trigger,
)
from sc.run.soft_checkin import (
    SoftCheckinOutcome,
    render_soft_checkin,
)


def _pref(action: PreferenceAction) -> Preference:
    return Preference(
        trigger=Trigger(),
        condition=Condition(),
        action=action,
        scope=Scope(),
        lifecycle=Lifecycle(),
    )


class SoftCheckinEnumTests(unittest.TestCase):
    def test_soft_checkin_is_stricter_than_auto_apply(self) -> None:
        forced = force_action_from_preferences(
            (_pref(PreferenceAction.AUTO_APPLY), _pref(PreferenceAction.SOFT_CHECKIN))
        )
        self.assertEqual(forced, PreferenceAction.SOFT_CHECKIN)

    def test_full_checkin_overrides_soft_checkin(self) -> None:
        forced = force_action_from_preferences(
            (_pref(PreferenceAction.SOFT_CHECKIN), _pref(PreferenceAction.FULL_CHECKIN))
        )
        self.assertEqual(forced, PreferenceAction.FULL_CHECKIN)


class SoftCheckinRenderTests(unittest.TestCase):
    """Non-tty smoke tests — the renderer must not hang and must report
    no-intervention when there's no stdin (pytest environment)."""

    def test_does_not_raise_on_minimal_input(self) -> None:
        # window=0 so this returns immediately.
        outcome = render_soft_checkin(
            stage="apply",
            files=["a.py"],
            reason=None,
            window_seconds=0.0,
        )
        self.assertIsInstance(outcome, SoftCheckinOutcome)
        self.assertFalse(outcome.intervened)

    def test_handles_many_files_without_hanging(self) -> None:
        outcome = render_soft_checkin(
            stage="apply",
            files=[f"file_{i}.py" for i in range(20)],
            reason="failure-signal trigger matched this session",
            window_seconds=0.0,
        )
        self.assertFalse(outcome.intervened)

    def test_handles_no_files(self) -> None:
        outcome = render_soft_checkin(
            stage="apply",
            files=[],
            reason="test",
            window_seconds=0.0,
        )
        self.assertFalse(outcome.intervened)


if __name__ == "__main__":
    unittest.main()
