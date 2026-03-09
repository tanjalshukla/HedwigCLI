from task_api.api import create_task_handler, list_tasks_handler, update_task_status_handler


def test_list_tasks_returns_success_envelope() -> None:
    body, status = list_tasks_handler({})
    assert status == 200
    assert body["ok"] is True
    assert "tasks" in body["data"]


def test_create_task_rejects_empty_title() -> None:
    body, status = create_task_handler({"title": "   "})
    assert status == 400
    assert body == {
        "ok": False,
        "error": {
            "code": "empty_title",
            "message": "title cannot be empty",
        },
    }


def test_update_task_status_rejects_invalid_status() -> None:
    body, status = update_task_status_handler("task-1", {"status": "archived"})
    assert status == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_status"
