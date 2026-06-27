# AGENTS.md

Working agreement for AI coding agents in this repo. Hedwig's own instructions
live in [`CLAUDE.md`](CLAUDE.md) — **read it before writing code.** This file
is the tool-agnostic pointer to it (the cross-tool `AGENTS.md` convention).

## The short version

- **Architecture reference:** [`SPEC.md`](SPEC.md)
- **Learn the codebase:** [`HEDWIG_END_TO_END.md`](HEDWIG_END_TO_END.md) — a narrative walkthrough plus a file-by-file reading list.
- **How to work + non-negotiable invariants:** [`CLAUDE.md`](CLAUDE.md)

## The verify loop

Before calling any change done:

```bash
make verify   # ruff lint + full test suite + plugin vendor-sync check
```

After editing anything under `sc/` that the plugin uses, regenerate the
vendored copy (`make verify` will fail until you do):

```bash
make sync-vendor
```

## Four invariants you cannot break

Full text in [`CLAUDE.md`](CLAUDE.md). In brief: no synthetic training data;
"learned" means the `PolicyClassifier` only; preferences are per-repo, not
per-developer; preferences tighten by default (one narrow loosening exception).
The model is untrusted — risk assessment is deterministic and
model-independent.
