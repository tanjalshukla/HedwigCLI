from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LastContext:
    logic_notes: list[str] = field(default_factory=list)
    guidelines: list[str] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    task_text: str = ""
    summary: str = ""

    def total(self) -> int:
        return len(self.logic_notes) + len(self.guidelines) + len(self.feedback)


_LAST = LastContext()


def record(
    *,
    logic_notes: list[str],
    guidelines: list[str],
    feedback: list[str],
    task_text: str,
    summary: str = "",
) -> None:
    global _LAST
    _LAST = LastContext(
        logic_notes=list(logic_notes),
        guidelines=list(guidelines),
        feedback=list(feedback),
        task_text=task_text,
        summary=summary,
    )


def last() -> LastContext:
    return _LAST
