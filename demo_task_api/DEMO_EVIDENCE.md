# Demo Evidence Checklist

Use this after one clean rehearsal or recording run.

## Exports to keep

From session 1:

```bash
sc observe export --session-id <SESSION_1_ID> --out .sc/exports/session1
```

From session 2:

```bash
sc observe export --session-id <SESSION_2_ID> --out .sc/exports/session2
```

Keep these files for the paper:
- `.sc/exports/session1/<SESSION_1_ID>_bundle.json`
- `.sc/exports/session1/<SESSION_1_ID>_traces.csv`
- `.sc/exports/session2/<SESSION_2_ID>_bundle.json`
- `.sc/exports/session2/<SESSION_2_ID>_traces.csv`

## Screenshots to capture

1. `Spec-aware intent summary`
   - show the plan after `--spec`
   - include the planned files and spec-aligned task summary

2. `Model-initiated architectural check-in`
   - capture the options, assumptions, and recommendation
   - this is the strongest screenshot in the demo

3. `Learned preferences before session 2`
   - run:
     ```bash
     sc observe preferences
     ```
   - show that your session-1 preference persisted

4. `Session 2 reduced-interruption run`
   - capture the fact that low-risk work proceeds with fewer interruptions

5. `Observability close`
   - either:
     ```bash
     sc observe report
     ```
   - or:
     ```bash
     sc observe traces --limit 10
     ```

## Small numbers to extract for the paper

From the exported bundles or `sc observe checkin-stats`:
- number of model-initiated check-ins in session 1
- number of policy-initiated check-ins in session 1
- number of total check-ins in session 2
- whether session 2 preserved the same API/interface constraints without re-asking the same question

Good qualitative example to quote in the paper:
- the exact preference you gave in session 1
- the evidence that it shaped session 2 behavior

## What not to overclaim

- do not claim the system learned an optimal policy
- do not claim the heuristic weights are validated
- do claim that the trace-driven loop changed the second session's prompt/policy context
