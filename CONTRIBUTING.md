# Contributing to Hedwig

Thanks for your interest. Hedwig is a governance layer for Claude Code — it
decides, per file edit, whether to auto-apply or surface for review, and
calibrates that line per-repo from outcomes. Contributions that sharpen that
core are welcome.

## Before you start

- Read [`SPEC.md`](SPEC.md) for the architecture, the decision cascade, and the
  policy-weight table.
- Read [`HEDWIG_END_TO_END.md`](HEDWIG_END_TO_END.md) for a narrative walkthrough.
- The governance core lives in `sc/`. The Claude Code plugin in `plugin/`
  **vendors** a slim copy of `sc/` into `plugin/vendor/sc/` so it installs
  standalone — never hand-edit `plugin/vendor/`; edit `sc/` and regenerate.

## Development setup

```bash
make install        # editable install + dev deps into .venv
make test           # full test suite
make lint           # ruff (undefined names, unused imports, syntax)
make verify         # lint + tests + plugin vendor-sync check — the done-criterion
make sync-vendor    # regenerate plugin/vendor/sc from sc/ (after editing sc/)
```

## The one rule that trips people up

**After editing anything in `sc/` that the plugin uses, run `make sync-vendor`.**
Otherwise the plugin runs stale code, and `make verify` will fail on the
vendor-drift check. CI enforces this.

## Submitting a change

1. Branch from `main`.
2. Make the change. Keep it surgical — match the surrounding style.
3. Add or update tests in `tests/`. New seams get their own test file.
4. `make verify` green (and `make sync-vendor` if you touched `sc/`).
5. If you changed anything under `plugin/`, bump the `version` in
   `plugin/.claude-plugin/plugin.json` — installed copies only update when the
   version rises, and CI fails a plugin change without a bump.
6. Open a PR describing what changed and why. CI runs `make verify` on every PR.

## Design principles to respect

- **Minimal, non-speculative changes.** No features beyond what's asked, no
  abstractions for single-use code.
- **The model is untrusted.** Risk assessment is deterministic and
  model-independent on purpose — don't make a security decision dependent on
  model output.
- **"Learned" means the online classifier specifically** — not the heuristic
  scorer, preference inference, or threshold adaptation. Keep that precise in
  code and docs.
- **Preferences are per-repo, not per-developer**, and they add caution by
  default. See `SPEC.md` for the one narrow exception.

## Reporting bugs / requesting features

Open an issue using the templates in `.github/ISSUE_TEMPLATE/`. For security
issues, follow [`SECURITY.md`](SECURITY.md) instead of opening a public issue.
