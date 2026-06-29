# Hedwig (Claude Code plugin)

Governance layer for Claude Code. Hedwig watches the agent's file edits and decides — per edit, per session — which ones need your eyes and which don't. Low-risk edits apply automatically; anything worth a look gets surfaced with a plain-English reason.

The governance core (risk scoring, the classifier, SQLite trace store, regret loop) runs locally in Python. No API key, no AWS, nothing leaves your machine.

## Install

```bash
claude plugin marketplace add tanjalshukla/HedwigCLI
claude plugin install hedwig@hedwig-marketplace
```

Or from a local checkout:

```bash
claude --plugin-dir ./plugin
```

To update an installed copy to the latest release:

```bash
claude plugin update hedwig@hedwig-marketplace
```

## Make the learned scorer run

By default, hooks run under a bare `python3` that may not have `numpy` or `scikit-learn`. The heuristic scorer always works without them, but to get the online classifier, run `/hedwig-setup` in Claude Code (or, from a local checkout, `python3 plugin/bin/hedwig-setup.py`).

This builds a small dedicated venv at `~/.hedwig/venv` and the hooks re-exec under it automatically. No shell config needed; works in any terminal.

## How it learns from outcomes

Claude Code owns the native approve/deny prompt — a user's click there is invisible to hooks. So Hedwig learns from what actually happens to an auto-applied edit:

- **Positive** — the edit survives the session.
- **Regret** — the agent reverts an edit Hedwig auto-applied (detected without needing a test command), or an end-of-turn verification fails on files in the change. Each regret tightens the next similar edit on that file *and* feeds a corrective gradient to the classifier, generalizing to risk-similar edits on other files.

## Hooks

- **`PreToolUse`** — scores each Edit/Write/MultiEdit through the cascade, applies the confidence handshake (tighten-only), then either suppresses the native prompt (auto-apply) or passes through for your review.
- **`PostToolUse`** — records the executed edit; detects reversals and routes them as regret.
- **`Stop`** — runs end-of-turn verification when `HEDWIG_VERIFY_CMD` is configured; records verification failures as regret.

## Commands

- `/hedwig-status` — suppressed vs. surfaced this session, with reasons
- `/hedwig-weights` — classifier drift from cold-start (▲▼ per feature)
- `/hedwig-retrospective` — regret events
- `/hedwig-learn` — review and confirm a noticed behavioral pattern
- `/hedwig-rules` — view or set hard constraints
- `/hedwig-scan` — flag security-sensitive files keyword matching misses (agent-reasoned)
- `/hedwig-setup` — build the learned-scorer interpreter (once per machine)

## State

Everything lives in `${CLAUDE_PLUGIN_DATA}` (managed by Claude Code):

- `trust.db` — SQLite: decision traces + persisted classifier
- `decisions.jsonl` — per-edit verdict log (for `/hedwig-status`)
- `regret.jsonl` — reversal and verification-failure events
- `traces.jsonl` — human-readable mirror of executed-action traces

## Releasing an update

Installed copies are pinned to a git SHA; users only pull changes when the
plugin version increases. So every user-facing change ships with a bump:

1. Make the change (run `make sync-vendor` if you edited `sc/`).
2. Bump `version` in `.claude-plugin/plugin.json`.
3. `make verify` green, then push to `main`.

CI enforces this: a PR that changes anything under `plugin/` without a version
bump fails `tooling/check_plugin_version_bump.py`. Users update with
`claude plugin update hedwig@hedwig-marketplace`.
