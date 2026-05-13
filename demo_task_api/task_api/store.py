from __future__ import annotations

from task_api.models import Task

_TASKS: dict[str, Task] = {
    "task-1": Task(id="task-1", title="Design auth middleware", status="in_progress", priority="high"),
    "task-2": Task(id="task-2", title="Write API documentation", status="todo", priority="medium"),
    "task-3": Task(id="task-3", title="Set up CI pipeline", status="done", priority="low"),
}


def list_tasks(
    status: str | None = None,
    priority: str | None = None,
) -> list[Task]:
    tasks = list(_TASKS.values())
    if status:
        tasks = [t for t in tasks if t.status == status]
    if priority:
        tasks = [t for t in tasks if t.priority == priority]
    return tasks


def get_task(task_id: str) -> Task | None:
    return _TASKS.get(task_id)


def save_task(task: Task) -> None:
    _TASKS[task.id] = task


def remove_task(task_id: str) -> bool:
    return _TASKS.pop(task_id, None) is not None


def next_task_id() -> str:
    n = len(_TASKS) + 1
    while f"task-{n}" in _TASKS:
        n += 1
    return f"task-{n}"
