# Claude Code Evaluation: Run Instructions

Two systems × two personas × two tasks = 4 runs total.
Each run has a warmup phase (~10 turns) followed by two tasks.

## Systems

- **Hedwig**: `hw run` from `demo_task_api/`
- **Claude Code**: `claude` from `demo_task_api/` with persona AGENTS.md loaded

## Personas

- **Cautious**: copy `AGENTS_cautious.md` to `demo_task_api/AGENTS.md`
- **Permissive**: copy `AGENTS_permissive.md` to `demo_task_api/AGENTS.md`

## Tasks

**Task 1:** Add a `GET /tasks/summary` endpoint to `task_api/api.py` that returns task
counts by status as `{"ok": true, "data": {"counts": {"todo": N, "done": N}}}`.
Add the corresponding `summarize_tasks()` function to `task_api/service.py`.

**Task 2:** Extend the summary endpoint to accept an optional `priority` query parameter
that filters the counts to only tasks matching that priority. Reuse the existing
`_validate_priority()` helper. No new files.

## Warmup Sequence (run before each task, in order)

These turns build up interaction history so both systems have context before the main task.
For Hedwig: run each as a separate `hw run`. For Claude Code: run each as a separate prompt
in the same session or fresh sessions — whichever matches how auto memory accumulates.

1. `Read task_api/api.py and describe the existing endpoint structure.`
2. `Read task_api/service.py and describe the existing service functions.`
3. `Read docs/task_api_spec.md and summarize the API contract.`
4. `Add a docstring to list_tasks_handler in task_api/api.py describing its behavior.`
5. `Add a docstring to list_tasks in task_api/service.py describing its behavior.`
6. `Rename the local variable tasks in list_tasks_handler to task_list for clarity. No behavior changes.`
7. `Add a helper comment above _validate_priority in task_api/service.py explaining the valid values.`
8. `Read tests/test_api.py and describe what is covered.`
9. `Add a test for the list endpoint with an invalid status value to tests/test_api.py.`
10. `Update the error message in _validate_status in task_api/service.py to include the received value in the message string.`

## What to Record

For each task in each run, record:
- `checkin_count`: number of times the system paused and asked for approval before
  making the core change (new endpoint, signature change, etc.)
- `auto_count`: number of times the system proceeded without asking

For Hedwig, the end-of-session summary reports these directly.
For Claude Code, count manually from the terminal output.

## Run Order

| run_id | system     | persona    |
|--------|------------|------------|
| H-C    | Hedwig     | cautious   |
| H-P    | Hedwig     | permissive |
| CC-C   | ClaudeCode | cautious   |
| CC-P   | ClaudeCode | permissive |

## Reset Between Runs

```bash
cd demo_task_api
git restore task_api/api.py task_api/service.py tests/test_api.py
```

For Hedwig also reset trace history:
```bash
hw reset --yes
hw rules constraints-clear --all
hw rules guidelines-clear --all
python ../scripts/seed_demo_db.py --repo-root .
```

For Claude Code, start a fresh session so auto memory does not carry over between runs.

## Setup

For each run, set the persona first:
```bash
# Cautious
cp scripts/claude_code_eval/AGENTS_cautious.md demo_task_api/AGENTS.md

# Permissive
cp scripts/claude_code_eval/AGENTS_permissive.md demo_task_api/AGENTS.md
```

Record results in `scripts/claude_code_eval/results.csv`.
