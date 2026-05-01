# Demo Task API

This directory contains the small task-tracking backend used in the
Hedwig demo video.

It is intentionally small, but it still has enough structure to show:

- route handlers and response envelopes in `task_api/api.py`
- business logic and validation in `task_api/service.py`
- structured user-facing errors in `task_api/errors.py`
- a simple in-memory store in `task_api/store.py`
- a short task reference document in `docs/task_api_spec.md`
- tests in `tests/test_api.py`

The fixture is designed for a two-session demo:

1. Session 1 adds a new summary endpoint.
2. Session 2 extends that endpoint with an optional priority filter.

The important part of the demo is not the backend itself. It is how
Hedwig changes its oversight between the two sessions.

The committed fixture starts at the pre-session1 baseline, so a fresh
clone reproduces the same starting point used in the demo video.

## Reproducing the Demo Video

Use [`DEMO_FLOW.md`](DEMO_FLOW.md).

That document contains:

- off-camera setup
- exact commands
- expected prompts
- what to approve
- what should appear on screen
- which moments to pause on for screenshots or video capture

## Quick Verification

From the repository root, you can verify the fixture before filming:

```bash
PYTHONPATH=demo_task_api .venv/bin/python -m pytest demo_task_api/tests -q
```

## Resetting the Demo Fixture

To return the code to the pre-session1 baseline:

```bash
git restore task_api/api.py task_api/service.py
```

To reset Hedwig's local state for the demo repo:

```bash
hw reset --yes
hw rules constraints-clear --all
hw rules guidelines-clear --all
python ../scripts/seed_demo_db.py --repo-root .
```

The seed step pre-loads 14 synthetic developer decisions so that
`hw observe weights` shows visible coefficient drift from the first
`hw observe weights` call. See [`DEMO_FLOW.md`](DEMO_FLOW.md) for details.

The clean pre-session1 baseline does not contain `summary_handler` in
`task_api/api.py` or `get_task_summary` in `task_api/service.py`.
