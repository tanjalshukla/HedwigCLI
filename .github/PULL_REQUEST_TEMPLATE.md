## What this changes
A short description of the change and why.

## Checklist
- [ ] `make verify` is green (lint + tests + vendor-sync).
- [ ] If I edited `sc/`, I ran `make sync-vendor` and committed the vendored copy.
- [ ] If I changed anything under `plugin/`, I bumped `version` in
      `plugin/.claude-plugin/plugin.json`.
- [ ] Tests added/updated in `tests/` for the behavior I changed.
- [ ] The change is surgical and matches surrounding style.

## Invariants (confirm none are violated)
- [ ] Risk assessment stays deterministic / model-independent.
- [ ] "Learned" still refers only to the online classifier.
- [ ] Preferences remain per-repo and caution-by-default.

## Notes for the reviewer
Anything non-obvious — design tradeoffs, follow-ups, things you're unsure about.
