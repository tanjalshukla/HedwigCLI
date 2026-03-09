from task_api.models import Task

_TASKS: dict[str, Task] = {
    "task-1": Task(id="task-1", title="Draft demo outline", status="in_progress", priority="high"),
    "task-2": Task(id="task-2", title="Book advisor rehearsal", status="todo", priority="medium"),
    "task-3": Task(id="task-3", title="Submit camera-ready assets", status="done", priority="low"),
}


def list_tasks() -> list[Task]:
    return list(_TASKS.values())


def get_task(task_id: str) -> Task | None:
    return _TASKS.get(task_id)


def save_task(task: Task) -> None:
    _TASKS[task.id] = task


def remove_task(task_id: str) -> bool:
    return _TASKS.pop(task_id, None) is not None


def next_task_id() -> str:
    return f"task-{len(_TASKS) + 1}"
