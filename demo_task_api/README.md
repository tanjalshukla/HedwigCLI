# Demo Task API

This is a small task-tracking API fixture for Smart Coder demos.

It is intentionally small, but it has enough structure to show:
- spec-aware planning
- interface-sensitive changes in `task_api/api.py`
- model-initiated architectural check-ins
- preference learning across sessions
- safety boundaries via `locked/`

Main files:
- `task_api/api.py` - route handlers and response envelopes
- `task_api/service.py` - business logic and validation
- `task_api/store.py` - in-memory task store
- `task_api/errors.py` - structured application errors
- `docs/task_api_spec.md` - task constraints for the demo
- `DEMO_RULES.md` - Smart Coder rule import file
