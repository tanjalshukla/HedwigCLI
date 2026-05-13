# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.

## What this workspace is

A parallel track for Hedwig (`../sc/`), a coding-agent governance layer with a preference taxonomy. This workspace exists to **mine the SWE-chat dataset for preference signals in real developer-agent traces** so the taxonomy can be revised based on evidence instead of guesses.

**Read `BRIEF.md` first.** It is authoritative. This file is orientation.

## What you are and are not doing

You are:

- Extracting behavioral features from 62K turns across 5,776 SWE-chat sessions
- Running analyses that answer five specific research questions about real preference signals (see `BRIEF.md`)
- Producing a findings report (`docs/FINDINGS.md`) and a taxonomy-revision summary (`docs/SCHEMA_IMPLICATIONS.md`)

You are not:

- Validating Hedwig's current taxonomy against SWE-chat labels. That was the previous (wrong) task.
- Modifying `../sc/` in any way
- Training ML models. Logistic regression, decision trees, k-means, TF-IDF only.
- Inventing features you can't actually compute from SWE-chat fields
- Writing paper-ready prose — your findings feed the paper, but writing them is the user's job

## Dataset

Already fetched. See `data/DATASET.md`. Two tables matter: `sessions` (metadata, persona label) and `conversations` (turn-level pushback label, prompts, tool calls).

## Import pattern (if needed)

You generally should not need to import from `../sc/` for this task. The taxonomy we're informing is defined there, but you're not calling its inference functions — you're replacing them eventually. If you do need to look at the taxonomy's shape for context:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# read-only reference only; do not call
from sc.preferences import CodingMode, UserPersona, PushbackType
```

## Deliverable shape

```
swechat_agent/
├── BRIEF.md, CLAUDE.md, README.md
├── data/
│   ├── raw/                         # HuggingFace dump (already placed)
│   ├── features.parquet             # per-turn feature matrix
│   ├── clusters.json                # cluster assignments from Q3
│   └── feedback_topics.json         # Q5 output
├── docs/
│   ├── FINDINGS.md                  # primary deliverable — one section per research question
│   ├── FEATURE_CATALOG.md           # feature definitions + provenance
│   └── SCHEMA_IMPLICATIONS.md       # what Hedwig's taxonomy should change based on findings
└── scripts/
    ├── extract_features.py
    ├── predict_pushback.py          # Q1, Q2
    ├── cluster_personas.py          # Q3
    ├── session_trajectories.py      # Q4
    └── feedback_topics.py           # Q5
```

## Commands

```bash
# From swechat_agent/ root:

# Extract features once
python scripts/extract_features.py

# Then run each analysis
python scripts/predict_pushback.py
python scripts/cluster_personas.py
python scripts/session_trajectories.py
python scripts/feedback_topics.py

# If you need the Hedwig venv for sklearn/pandas:
../.venv/bin/python scripts/extract_features.py
```

## Workflow norms

1. **Read `BRIEF.md` end-to-end first.** The five research questions are load-bearing.
2. **Write `docs/FEATURE_CATALOG.md` before `scripts/extract_features.py`.** Forces you to confront what you can and cannot compute.
3. **Tackle one research question at a time.** Don't try to extract everything before running any analysis.
4. **Progressive commits.** Each question's output gets committed when it's done, not at the end.
5. **If a question has no signal, say so.** Negative findings are findings. Don't fabricate.
6. **If you get stuck, write to `docs/BLOCKERS.md` and stop.** Don't force a bad answer.
7. **The previous validation run is baseline context, not part of this task.** Don't cite or re-run it.

## What "done" looks like

`docs/FINDINGS.md` answers all five research questions, each with:
- A direct answer
- Supporting numbers (top features, cluster counts, verbatim examples)
- An implication for Hedwig's taxonomy

Plus `docs/SCHEMA_IMPLICATIONS.md` as a short summary of what should change in Hedwig's 5-dim preference schema given the findings.

That document is the point of the exercise.
