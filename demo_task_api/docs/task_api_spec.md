# Task API Spec

1. Preserve the existing success response envelope: `{"ok": true, "data": ...}`.
2. Preserve the existing error response envelope: `{"ok": false, "error": {"code": ..., "message": ...}}`.
3. Preserve public handler signatures in `task_api/api.py` unless explicitly approved.
4. Prefer `AppError` with explicit error codes over generic exceptions for user-facing failures.
5. Prefer small, local changes over broad abstractions unless duplication is substantial.
6. If a feature requires changing an existing route shape, response envelope, or handler signature, stop and check in with assumptions and options.
