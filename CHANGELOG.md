# Changelog

All notable changes to Hedwig are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the plugin version
lives in `plugin/.claude-plugin/plugin.json`.

## [0.1.8] — 2026-06-30

First public open-source release, accompanying the ACM CAIS 2026 demonstration.

### Added
- **Governance cascade** for Claude Code edits: hard constraints → heuristic /
  online-classifier scorer → confirmed preferences → agent confidence handshake
  → deterministic security floor → deny-with-reason self-correction gate.
- **Online logistic-regression scorer** (per-repo, SQLite-persisted) that takes
  over from the cold-start heuristic after 10 real decisions; isotonic-calibrated.
- **Regret loop** — auto-applied edits that are later reverted or fail
  verification become corrective negative signals, applied exactly once.
- **Semantic security scan** (`/hedwig-scan`) — an agent pass flags
  security-sensitive files keyword matching misses; additive-only, never
  weakens the deterministic keyword floor.
- **Repo-memory layer** — guidelines, logic notes, and constraints persisted on
  disk and injected into the agent's context per task.
- **Hypothesis bank** — proposes standing preferences from observed patterns;
  nothing changes behavior until the developer confirms it (`/hedwig-learn`).
- **Observability commands** — `/hedwig-status`, `/hedwig-weights`,
  `/hedwig-retrospective`, `/hedwig-rules`, `/hedwig-scan`, `/hedwig-setup`.
- Runs fully local: no API key, no cloud, all state on disk.

### Security
- Risk assessment is deterministic and model-independent; security-sensitive
  edits are held behind a floor the learned scorer cannot override.

[0.1.8]: https://github.com/tanjalshukla/HedwigCLI/releases/tag/v0.1.8
