from __future__ import annotations

from task_api.api import (
    create_task_handler,
    delete_task_handler,
    list_tasks_handler,
    summary_handler,
    update_task_status_handler,
)


def test_list_tasks_returns_all() -> None:
    body, status = list_tasks_handler({})
    assert status == 200
    assert body["ok"] is True
    assert len(body["data"]["tasks"]) >= 3


def test_list_filter_by_status() -> None:
    body, status = list_tasks_handler({"status": "done"})
    assert status == 200
    assert all(t["status"] == "done" for t in body["data"]["tasks"])


def test_list_filter_by_priority() -> None:
    body, status = list_tasks_handler({"priority": "high"})
    assert status == 200
    assert all(t["priority"] == "high" for t in body["data"]["tasks"])


def test_list_rejects_invalid_status() -> None:
    body, status = list_tasks_handler({"status": "archived"})
    assert status == 400
    assert body["error"]["code"] == "invalid_status"


def test_create_task_success() -> None:
    body, status = create_task_handler({"title": "New task", "priority": "low"})
    assert status == 201
    assert body["data"]["task"]["title"] == "New task"


def test_create_task_rejects_empty_title() -> None:
    body, status = create_task_handler({"title": "   "})
    assert status == 400
    assert body["error"]["code"] == "empty_title"


def test_create_task_rejects_long_title() -> None:
    body, status = create_task_handler({"title": "x" * 201})
    assert status == 400
    assert body["error"]["code"] == "title_too_long"


def test_update_status_success() -> None:
    body, status = update_task_status_handler("task-1", {"status": "done"})
    assert status == 200
    assert body["data"]["task"]["status"] == "done"


def test_update_status_rejects_invalid() -> None:
    body, status = update_task_status_handler("task-1", {"status": "archived"})
    assert status == 400
    assert body["error"]["code"] == "invalid_status"


def test_update_status_rejects_missing_task() -> None:
    body, status = update_task_status_handler("task-999", {"status": "done"})
    assert status == 404


def test_delete_success() -> None:
    body, status = delete_task_handler("task-3")
    assert status == 200


def test_summary() -> None:
    body, status = summary_handler({})
    assert status == 200
    counts = body["data"]["counts"]
    assert set(counts.keys()) == {"todo", "in_progress", "done"}
