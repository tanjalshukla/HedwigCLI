# Security Policy

Hedwig is a governance layer for a coding agent, so we take its own security
posture seriously.

## What Hedwig does and doesn't touch

- **Runs fully local.** The plugin path makes no network calls and needs no API
  key or cloud credentials. All state lives on disk under
  `${CLAUDE_PLUGIN_DATA}` (SQLite + JSONL).
- **The model is treated as untrusted input.** Risk assessment (`assess_risk`)
  is deterministic and model-independent by design — a prompt-injected agent
  cannot talk Hedwig into scoring a risky edit as safe, and security-sensitive
  files are held behind a deterministic floor the learned scorer cannot
  override.
- **It governs file edits; it does not execute arbitrary code.** End-of-turn
  verification runs only the command a developer explicitly configures.

## Reporting a vulnerability

Please report security issues **privately**, not via a public GitHub issue.

- Email **shuklatanjal@gmail.com** with subject `HEDWIG SECURITY`, or
- Use GitHub's private vulnerability reporting (Security → "Report a
  vulnerability") on the repository.

Please include: what you found, how to reproduce it, the affected version
(`plugin/.claude-plugin/plugin.json` → `version`), and the potential impact.

We aim to acknowledge a report within a few days and to coordinate a fix and
disclosure timeline with you. This is a research project maintained by a small
team — thank you for your patience and for reporting responsibly.

## Supported versions

This is pre-1.0 software under active development. Security fixes are applied to
the latest released plugin version on `main`; there is no long-term support
branch yet.
