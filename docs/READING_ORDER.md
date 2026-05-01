# Hedwig Reading Order

This repo is small enough to read end to end, but the quickest path to
the core behavior is:

## 1. CLI entry points

1. `sc/__main__.py`
2. `sc/cli.py`
3. `sc/commands/admin.py`
4. `sc/commands/observe.py`

## 2. Governed run loop

1. `sc/run/command.py`
2. `sc/run/read_stage.py`
3. `sc/run/model.py`
4. `sc/run/apply_stage.py`
5. `sc/run/ui.py`
6. `sc/run/reporting.py`
7. `sc/run/helpers.py`

## 3. Policy, trust, and adaptation

1. `sc/policy.py`
2. `sc/ml_policy.py`
3. `sc/autonomy.py`
4. `sc/plan_gate.py`
5. `sc/trust_db.py`
6. `sc/prompt_builder.py`

## 4. Structured model protocol

1. `sc/schema.py`
2. `sc/agent_client.py`
3. `sc/checkin_quality.py`
4. `sc/session.py`
5. `sc/session_feedback.py`

## 5. Supporting modules

1. `sc/constraints.py`
2. `sc/features.py`
3. `sc/patch.py`
4. `sc/verification.py`
5. `sc/repo.py`
6. `sc/config.py`
7. `sc/cli_shared.py`

## 6. Demo fixture

1. `demo_task_api/README.md`
2. `demo_task_api/DEMO_FLOW.md`
3. `demo_task_api/task_api/api.py`
4. `demo_task_api/task_api/service.py`
5. `demo_task_api/tests/test_api.py`

## 7. Focused tests

1. `tests/test_ml_policy.py`
2. `tests/test_run_history_context.py`
3. `tests/test_run_reporting.py`
4. `tests/test_run_ui.py`
5. `demo_task_api/tests/test_api.py`
