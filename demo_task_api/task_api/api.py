from task_api.errors import AppError
from task_api.models import Task
from task_api.service import create_task, delete_task, list_tasks, summarize_tasks, update_task_status


def list_tasks_handler(query: dict[str, str | None]) -> tuple[dict[str, object], int]:
    try:
        tasks = list_tasks(status=query.get("status"), priority=query.get("priority"))
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"tasks": [_task_to_dict(task) for task in tasks]}), 200

def create_task_handler(payload: dict[str, str]) -> tuple[dict[str, object], int]:
    try:
        task = create_task(title=payload.get("title", ""), priority=payload.get("priority", "medium"))
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"task": _task_to_dict(task)}), 201


def update_task_status_handler(task_id: str, payload: dict[str, str]) -> tuple[dict[str, object], int]:
    try:
        task = update_task_status(task_id=task_id, status=payload.get("status", ""))
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"task": _task_to_dict(task)}), 200


def delete_task_handler(task_id: str) -> tuple[dict[str, object], int]:
    try:
        delete_task(task_id)
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"deleted": True}), 200


def summary_handler(query: dict[str, str | None]) -> tuple[dict[str, object], int]:
    try:
        counts = summarize_tasks(priority=query.get("priority"))
    except AppError as exc:
        return exc.to_response(), exc.status_code
    return _ok({"counts": counts}), 200


def _ok(data: dict[str, object]) -> dict[str, object]:
    return {
        "ok": True,
        "data": data,
    }


def _task_to_dict(task: Task) -> dict[str, str]:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
    }
