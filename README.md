# Hedwig

Hedwig is a governance layer for Claude Code. For every file edit the agent proposes, it decides whether to apply it automatically or pause for your review — and adjusts where it draws that line based on what happened in this repo.

> 1 of 10 award-winning demo papers — ACM Conference on AI and Agentic Systems (CAIS) 2026.

---

## The problem

Coding agents either interrupt constantly or run wide open. Neither adapts. You end up approving trivial edits on autopilot, or finding out after the fact that something you'd have wanted to see got applied silently.

Hedwig's take: oversight should calibrate from how you actually work in this repo, not from a config you wrote once.

---

## How it works

Every file edit passes through a five-layer decision:

1. **Hard rules** — path-level constraints (`always_deny`, `always_check_in`). Non-negotiable, override everything.
2. **Trust grants** — temporary leases from prior approve+remember decisions. Familiar files skip the queue.
3. **Threshold adjustment** — four signals shift the auto-apply bar before scoring: session engagement, coding mode, model calibration, and persistent mode. Grounded in SWE-chat empirical findings.
4. **Decision model** — deterministic risk signals (diff size, blast radius, change pattern, security sensitivity) feed a heuristic scorer cold, then an online logistic-regression classifier once 10 real decisions accumulate.
5. **Preference override** — confirmed behavioral patterns tighten the verdict further.

The cascade runs locally in Python. Nothing is sent anywhere.

---

## What it learns

- **Approve/deny decisions** → online classifier training signal. Shifts which file/change patterns auto-apply vs. pause.
- **Regret events** → when an auto-applied edit gets reverted or fails verification, that becomes a corrective gradient applied exactly once.
- **Behavioral patterns** → the hypothesis bank accumulates evidence over sessions and surfaces one candidate when confidence is high enough. You confirm or decline. Nothing fires until you confirm.
- **Repo memory** → hard rules, style guidelines, logic notes, and past corrections persist and are retrieved semantically into the agent's context before each task.

Developer style is not stable across sessions (SWE-chat ICC = 0.249). Hedwig learns per-repo, not per-person.

---

## Install

### Claude Code plugin (no credentials, no cloud)

```bash
claude plugin marketplace add tanjalshukla/HedwigCLI
claude plugin install hedwig@hedwig-marketplace
```

Already installed? Pull the latest release with `claude plugin update hedwig@hedwig-marketplace`.

Make an edit, then run `/hedwig-status` to see what was auto-applied vs. surfaced and why.

For the learned scorer, build a dedicated interpreter once — run `/hedwig-setup`
in Claude Code. Without it the plugin runs the heuristic scorer; with it, the
online classifier runs on every edit.

| Capability | Plugin |
|---|---|
| Risk scoring + auto-apply / surface / deny | ✅ |
| Online logistic-regression classifier | ✅ (after `/hedwig-setup`) |
| Regret loop (reversal + verification failure) | ✅ |
| Hard constraints (`/hedwig-rules`) | ✅ |
| Repo memory layer | ✅ |
| Semantic security scan (`/hedwig-scan`) | ✅ |
| Hypothesis bank + confirmation (`/hedwig-learn`) | ✅ |
| Confidence handshake (agent self-pause) | ✅ |
| Threshold adaptation + session signals | 🔜 |

The plugin learns from edit **outcomes** — reversals and verification failures — not approve/deny clicks (Claude Code owns the native prompt and doesn't expose it to hooks). Same learning loop, different signal source.

### Research CLI (Bedrock-backed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install --no-build-isolation -e .
aws sso login --profile <PROFILE>
hw init --model-id <inference-profile-arn> --region us-east-1
hw
```

---

## Observability

```bash
/hedwig-status        # suppressed vs. surfaced this session, with reasons
/hedwig-weights       # classifier drift from cold-start (▲▼ per feature)
/hedwig-retrospective # regret events — where it was too permissive
/hedwig-learn         # review and confirm a noticed behavioral pattern
/hedwig-rules         # view or set hard constraints
/hedwig-scan          # flag security-sensitive files keyword matching misses
```

---

## Repository layout

```
sc/                   # governance core (shared by CLI and plugin)
  features.py         # deterministic risk assessment
  policy.py           # PolicyScorer seam: heuristic + learned adapters
  ml_policy.py        # online logistic regression + isotonic calibration
  hypothesis_bank.py  # evidence accumulation and pattern surfacing
  preferences.py      # 5-dim preference taxonomy + matching
  regret.py           # regret detection and correction
  trust_db.py         # SQLite facade
  store/              # trace, rule, lease, preference, model stores
  run/                # REPL, read/apply cascade, UI

plugin/               # Claude Code plugin: hooks + vendored governance core
demo_recipe_api/      # demo fixture (recipe REST API)
tests/                # 453 tests — run with `make test`
```

---

## Further reading

- [`plugin/README.md`](plugin/README.md) — plugin install, hooks, and how outcome-based learning works
- [`HEDWIG_END_TO_END.md`](HEDWIG_END_TO_END.md) — architecture walkthrough and file-by-file reading list
- [`SPEC.md`](SPEC.md) — vocabulary, cascade detail, policy weights, design decisions
