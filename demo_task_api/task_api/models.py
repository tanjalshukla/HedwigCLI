from dataclasses import dataclass
from typing import Literal

TaskStatus = Literal["todo", "in_progress", "done"]
TaskPriority = Literal["low", "medium", "high"]


@dataclass(slots=True)
class Task:
    id: str
    title: str
    status: TaskStatus = "todo"
    priority: TaskPriority = "medium"
