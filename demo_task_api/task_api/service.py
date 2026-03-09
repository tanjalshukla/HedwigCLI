from task_api.errors import AppError
from task_api.models import Task, TaskPriority, TaskStatus
from task_api.store import get_task, list_tasks as load_tasks, next_task_id, remove_task, save_task

VALID_STATUSES = {"todo", "in_progress", "done"}
VALID_PRIORITIES = {"low", "medium", "high"}


def list_tasks(status: str | None = None, priority: str | None = None) -> list[Task]:
    tasks = load_tasks()
    if status is not None:
        _validate_status(status)
        tasks = [task for task in tasks if task.status == status]
    if priority is not None:
        _validate_priority(priority)
        tasks = [task for task in tasks if task.priority == priority]
    return tasks


def create_task(title: str, priority: str = "medium") -> Task:
    _validate_title(title)
    _validate_priority(priority)
    task = Task(id=next_task_id(), title=title.strip(), priority=priority)
    save_task(task)
    return task


def update_task_status(task_id: str, status: str) -> Task:
    task = _require_task(task_id)
    _validate_status(status)
    task.status = status
    save_task(task)
    return task


def delete_task(task_id: str) -> None:
    _require_task(task_id)
    remove_task(task_id)


def _require_task(task_id: str) -> Task:
    task = get_task(task_id)
    if task is None:
        raise AppError(code="task_not_found", message=f"Task '{task_id}' was not found.", status_code=404)
    return task


def _validate_title(title: str) -> None:
    if not isinstance(title, str):
        raise AppError(code="invalid_title_type", message="title must be a string")
    if not title.strip():
        raise AppError(code="empty_title", message="title cannot be empty")


def _validate_status(status: str) -> TaskStatus:
    if status not in VALID_STATUSES:
        raise AppError(code="invalid_status", message=f"status must be one of {sorted(VALID_STATUSES)}")
    return status  # type: ignore[return-value]


def _validate_priority(priority: str) -> TaskPriority:
    if priority not in VALID_PRIORITIES:
        raise AppError(code="invalid_priority", message=f"priority must be one of {sorted(VALID_PRIORITIES)}")
    return priority  # type: ignore[return-value]
