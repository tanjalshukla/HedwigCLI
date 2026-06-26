# Hedwig (Claude Code plugin)

Trust runtime for Claude Code. Hedwig watches the agent's proposed file edits and decides — **learning from outcomes, not clicks** — which need your eyes and which don't. Low-risk edits are auto-applied (the native permission prompt is suppressed); the ones worth a look are surfaced with a plain-English reason. Same protocol whether the session is one edit or a long autonomous loop.

The governance core it needs (risk assessment, the scorer cascade, the online classifier, the SQLite trace store, dense rule retrieval) is **vendored** into `plugin/vendor/sc/`, so the plugin installs standalone — it does not require the research repo to be present. Regenerate the vendored copy with `python plugin/sync_vendor.py`.

## Install (no credentials, no cloud)

Install from the Hedwig marketplace (the repo *is* the marketplace):

```bash
claude plugin marketplace add tanjalshukla/HedwigCLI
claude plugin install hedwig@hedwig-marketplace
```

Or, to try it from a local checkout without installing, launch Claude Code with
the plugin loaded for that session:

```bash
git clone https://github.com/tanjalshukla/HedwigCLI.git
claude --plugin-dir ./HedwigCLI/plugin
```

### Make the learned scorer always run (one command)

Claude Code runs the hooks under a bare `python3` that usually has no
`numpy` / `scikit-learn`, so by default the online classifier stays dormant and
Hedwig runs the heuristic. To guarantee the **learned** scorer runs on every
edit, build Hedwig's interpreter once:

```bash
python3 plugin/bin/hedwig-setup.py
```

This creates a small dedicated venv at `~/.hedwig/venv` with the learned-scorer
deps (`numpy`, `scikit-learn`, `fastembed` — no torch, no GPU) and installs
nothing globally. The hooks **auto-discover it and re-exec under it**, so the
classifier runs regardless of which interpreter Claude Code launched — no shell
config, works in any terminal, survives plugin updates. (`$HEDWIG_PYTHON`
overrides the path; `$HEDWIG_NO_REEXEC=1` forces heuristic-only.)

Without this step, every hook still **degrades gracefully** to the stdlib
heuristic — a governed edit always works on a bare interpreter; you just don't
get the learned scorer or semantic retrieval until a capable interpreter exists.

The full governance loop runs **locally, in Python, with no LLM access and no cloud** — `assess_risk`, the cascade, the SQLite trace store, the online logistic-regression classifier, and rule retrieval. No `ANTHROPIC_API_KEY`, no AWS, no Bedrock.

It is **SQLite-backed and learns locally.** The heuristic scorer carries the first ~10 decisions (cold-start); the online classifier then takes over (`select_scorer()`'s `ready()` gate), exactly as in the research CLI. This uses `numpy` + `scikit-learn` (and `fastembed` for semantic rule retrieval) on the Python that runs the hooks — **no torch, no GPU, no AWS.** If any of those are missing, every hook **degrades gracefully** to the stdlib heuristic / keyword retrieval rather than crashing — a governed edit still works on a bare interpreter; you just don't get the learned scorer or semantic retrieval until the deps are present.

## How it learns from outcomes

There are no clicks to learn from — Claude Code owns the native permission prompt, and a user's allow/deny there is invisible to hooks. So Hedwig learns from what actually happens to an auto-applied edit:

- **Positive** — an auto-applied edit that survives the session.
- **Regret (negative)** — the agent reverts an edit Hedwig auto-applied (detected with no test command needed), or a configured end-of-turn verification fails on a file in the failing change. Each regret tightens the next like-action on that file *and* feeds the classifier a corrective gradient that generalizes to risk-similar edits on other files.

The booth money-shot: *"Hedwig auto-approved this edit. You reverted it. Watch it get more cautious on the next one like it."*

## The confidence handshake (agent self-pause)

The `confidence-checkin` skill lets Claude declare low confidence (or explicitly request review) on a risky edit *before* it lands, by running `bin/hedwig-declare.py`. Hedwig honors that by surfacing the edit even when its own risk assessment would auto-apply. This is **tighten-only** — a declaration can never loosen a verdict to auto-apply. When Claude doesn't declare, behavior is unchanged. It works when the agent cooperates and degrades silently when it doesn't.

## Hooks

- **`PreToolUse`** (Edit/Write/MultiEdit) — score the action through the cascade, honor any confidence handshake (tighten-only), then auto-allow safe edits (suppressing the native prompt) or pass through to the native prompt for review.
- **`PostToolUse`** — record the executed action; detect a reversal of an auto-applied edit and route it as a regret.
- **`Stop`** — run end-of-turn verification when `HEDWIG_VERIFY_CMD` (or `verify_cmd.txt`) is configured; on failure, record regret scoped to the files in the failing change.
- **`/hedwig-status`** — the one observability surface: edits suppressed vs. surfaced this session, and *why* the surfaced ones surfaced (including the regret money-shot).

## State

Persistent state lives in `${CLAUDE_PLUGIN_DATA}` (managed by Claude Code; survives plugin updates):

- `trust.db` — SQLite (WAL mode): decision traces + the persisted classifier. The substrate everything reads from.
- `decisions.jsonl` — one row per governed edit (verdict, reason, scorer) for `/hedwig-status`.
- `self_checkins.jsonl` — agent confidence declarations (the handshake channel).
- `regret.jsonl` — recorded regret events (reversals / verification failures).
- `sentinel.jsonl` — per-event hook payload log (empirical hook-semantics record; safe to leave on).
- `traces.jsonl` — human-readable mirror of the executed-action traces.

Everything is local; nothing phones home.
