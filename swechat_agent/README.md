# SWE-chat parallel-track workspace

Isolated workspace for a parallel Claude Code agent mining the SWE-chat dataset to discover **what preference signals actually exist in real developer-agent traces**. Findings feed revision of Hedwig's preference taxonomy in `../sc/`.

Previous task (validation against SWE-chat's own labels) was the wrong scope. Its artifacts stay as baseline context but are not the current goal. Read `BRIEF.md` for the real task.

## Start here

Read **[BRIEF.md](BRIEF.md)** — the full task brief. It defines deliverables, data shape, constraints, and ground rules.

## Dataset

HuggingFace-hosted. The user has already fetched it; place it at `data/raw/` before running anything. Document the exact path and version in `data/DATASET.md`.

## Layout

```
swechat_agent/
├── README.md              # this file
├── BRIEF.md               # full instructions (read this)
├── data/
│   ├── raw/               # HuggingFace SWE-chat dump goes here
│   ├── sessions/          # extracted per-session JSONL (you produce)
│   ├── summary.json       # extraction summary (you produce)
│   └── validation.json    # inference-vs-groundtruth agreement (you produce)
├── scripts/
│   ├── extract.py         # raw → per-session JSONL (you write)
│   └── validate.py        # JSONL → agreement report (you write)
└── docs/
    └── SCHEMA_MAP.md      # SWE-chat field → Hedwig column map (you write)
```

## Ground rules (short version)

1. Do NOT modify anything in `../sc/`. You may only **import** from `sc.preference_inference` read-only.
2. Do NOT write into the Hedwig trust DB. Your JSONL lives under `data/`.
3. Do NOT invent fields. If SWE-chat lacks `edit_distance`, document the proxy you computed.
4. Surface dataset-format mismatches early. Don't force bad mappings.
5. Commit progressively. Small diffs.

## Hedwig taxonomy contract (what your output must feed)

Hedwig's inference module — `sc.preference_inference` — exposes:

- `summarize_session(rows) -> SessionSummary`
- `infer_coding_mode(summary) -> CodingMode` (human_only / collaborative / vibe)
- `infer_user_persona(summary) -> UserPersona` (expert_nitpicker / vague_requester / mind_changer / unknown)
- `classify_pushback(user_decision, edit_distance, feedback_text) -> PushbackType` (correction / rejection / failure_report / non_pushback)

Each row fed to `summarize_session` must have: `session_id`, `task`, `user_decision`, `edit_distance`, `user_feedback_text`.

SWE-chat's own ground-truth labels (mode, persona, pushback) must be carried on each row as `_swechat_mode`, `_swechat_persona`, `_swechat_pushback` so `validate.py` can compare.

## Deliverables at handoff

- `data/sessions/*.jsonl` — extracted sessions
- `data/summary.json` — coverage stats
- `data/validation.json` — agreement numbers
- `docs/SCHEMA_MAP.md` — field mapping + gaps
- A short markdown summary of findings: agreement rates per category, where Hedwig's thresholds disagree most with SWE-chat, recommended threshold adjustments.

## Running the agent

From this folder:

```bash
claude
```

The agent has `sc/` as a sibling import root. If it needs Hedwig's preference module:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from sc.preference_inference import (
    summarize_session,
    infer_coding_mode,
    infer_user_persona,
    classify_pushback,
)
from sc.preferences import CodingMode, UserPersona, PushbackType
```
