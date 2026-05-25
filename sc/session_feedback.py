from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from .schema import WorkflowPhase


@dataclass
class SessionFeedback:
    current_phase: WorkflowPhase
    _recent_decisions: deque[bool] = field(default_factory=lambda: deque(maxlen=8))
    _recent_corrections: deque[str] = field(default_factory=lambda: deque(maxlen=6))
    _recent_response_ms: deque[int] = field(default_factory=lambda: deque(maxlen=8))
    _recent_guidance: deque[str] = field(default_factory=lambda: deque(maxlen=6))
    _phase_transition_note: str | None = None

    def set_phase(self, phase: WorkflowPhase) -> None:
        if phase == self.current_phase:
            return
        self.current_phase = phase
        self._phase_transition_note = f"Phase transition: now in {phase}."

    def note_decision(
        self,
        approved: bool,
        *,
        change_patterns: Iterable[str] | None = None,
        response_time_ms: int | None = None,
        feedback_text: str | None = None,
    ) -> None:
        self._recent_decisions.append(approved)
        if response_time_ms is not None and response_time_ms > 0:
            self._recent_response_ms.append(response_time_ms)
        if feedback_text:
            normalized = " ".join(feedback_text.split()).strip()
            if normalized:
                self._recent_guidance.append(normalized[:220])
        if approved or not change_patterns:
            return
        for pattern in change_patterns:
            if not pattern:
                continue
            normalized = pattern.split(":", 1)[-1]
            self._recent_corrections.append(normalized)

    def build_and_consume_context(self) -> str:
        lines: list[str] = []

        recent_denials = sum(1 for decision in self._recent_decisions if not decision)
        if recent_denials:
            lines.append(
                f"{recent_denials} denied actions in recent session steps. Exercise extra caution."
            )

        if self._recent_decisions:
            approvals = sum(1 for decision in self._recent_decisions if decision)
            total = len(self._recent_decisions)
            lines.append(f"Recent approval rate: {approvals}/{total} decisions approved.")

        if self._recent_response_ms:
            avg_response = sum(self._recent_response_ms) / len(self._recent_response_ms)
            lines.append(f"Average recent review latency: {avg_response:.0f}ms.")

        if self._recent_corrections:
            corrected = list(dict.fromkeys(self._recent_corrections))
            lines.append(f"Recent corrections this session: {', '.join(corrected)}.")

        if self._recent_guidance:
            latest_guidance = list(dict.fromkeys(self._recent_guidance))[-2:]
            lines.append(f"Recent developer guidance: {' | '.join(latest_guidance)}.")

        approval_streak = self._approval_streak()
        if approval_streak >= 5:
            lines.append(f"{approval_streak} consecutive approvals. Continue with focused edits.")

        if self._phase_transition_note:
            lines.append(self._phase_transition_note)
            self._phase_transition_note = None

        return "\n".join(f"- {line}" for line in lines)

    def _approval_streak(self) -> int:
        streak = 0
        for decision in reversed(self._recent_decisions):
            if not decision:
                break
            streak += 1
        return streak
