# Hedwig Booth — Operator Reference

## Setup (once per day)

```bash
cd demo_recipe_api

# Terminal 1: the recipe app (visitors see this in browser)
python server.py
# → open http://localhost:5001

# Terminal 2: Hedwig
rm -f .sc/trust.db        # fresh start for first visitor
AWS_PROFILE=dev hw
hedwig> /doctor           # verifies AWS + Bedrock; do this BEFORE the booth opens
```

## Between visitors

The app is **cumulative across the conference** — each visitor's accepted changes stay in the code so the next visitor builds on top of them. Don't `git restore` between visitors.

Governance state (preferences, hypotheses, learned weights) also persists by default — Hedwig gets sharper as more visitors interact with it. That's a feature, not a leak.

```
hedwig> /reset-demo       # ONLY if state gets corrupted or for end-of-day reset
```

Use `/reset-demo` sparingly — daily, or if a session goes sideways. For ordinary visitor handoff, just let the next person sit down and start typing.

If a visitor's change broke the app (e.g. server crashed, tests fail):

```bash
git restore recipe_api/   # rewinds code only — keeps Hedwig's learned state
```

The browser at `localhost:5001` auto-refreshes every 3 seconds. Changes Hedwig makes appear live.

---

## What visitors do (5-minute booth narrative)

The arc: **autonomy → learned calibration → principled governance.** The Hedwig moment must fire by minute 1, not minute 3.

### Task #1 — surface governance immediately (60s)

```
hedwig> add a prep_time_minutes field to the Recipe model and seed values for all 4 recipes — only models.py and store.py
```

The explicit scope ("only models.py and store.py") forces a scope-narrow on task #1. Hedwig will plan, you approve, then approve a multi-file write. Browser updates with `prep_time_minutes` visible on all 4 recipes.

**Say:** *"Notice it asked before writing? Most agents don't. That's the governance layer. And see how it's showing the diff before applying — you know exactly what's changing."*

### Task #2 — repeat the pattern (60s)

```
hedwig> add a difficulty field (easy / medium / hard) and seed values for all 4 recipes — models.py and store.py only
```

Approve. Browser updates.

### Task #3 — trigger the hypothesis (90s)

```
hedwig> add tag filtering to list_recipes — accept a ?tag= query param
```

When Hedwig proposes touching routes or tests, **deny** with *"just service.py"*. After the third scope-narrow this session, the green `✦ hedwig · learning` panel fires.

**Say:** *"It noticed you keep narrowing scope. It's asking to make that a standing preference. That's the calibration in action — it's not hardcoded, it came from your decisions."*

Then run:

```
hedwig> /prefs
```

Show the confidence bar.

### Task #4 — governance value (60s, optional)

```
hedwig> enforce the API key in auth.py — unauthenticated requests get 401
```

Hedwig will check-in even though you've been approving things. **Say:** *"Some files always pause, no matter what it learned. That's the hard-constraint layer — auth, security, anything you flag."*

---

## Key demo moments to point out

| What the visitor sees | What to say |
|---|---|
| `◆ hedwig · decision · read` — files need approval | "Hedwig asks before reading. It doesn't just take access." |
| Diff shown before write approval | "You see exactly what changes before it happens." |
| `✓ verification passed` | "Tests ran automatically. It didn't just write — it checked." |
| Green `✦ hedwig · learning · I noticed a pattern` panel | "It noticed you've narrowed scope 3 times. It's asking if that's a standing preference." |
| `/prefs` showing confidence bars | "This is what Hedwig has learned so far this session." |
| `/observe weights` showing drift | "These are the features it's learned to weight differently from your decisions." |

---

## Troubleshooting

**Hypothesis panel not firing:** Need 3 denials with scope-constraint feedback. Add a 4th task with a scope narrowing if needed.

**AWS credentials expired:** `aws sso login --profile dev` — takes ~30s. Or run `/doctor` to verify before each session.

**Bedrock throttled / slow:** Hedwig now retries with backoff and shows a yellow status. If it gives up, just re-enter the same task.

**Server shows an error:** Usually a visitor's code change broke the import. `git restore recipe_api/` and refresh the page.

**Visitor wants to keep their changes:** They can — just don't run `git restore` before they leave.

---

## Pre-demo checklist

- [ ] Terminal 1: `python server.py`, browser open at `http://localhost:5001`, 4 seed recipes visible
- [ ] Terminal 2: `AWS_PROFILE=dev hw` running
- [ ] Run `/doctor` — confirms STS + Bedrock work end-to-end
- [ ] Run `/reset-demo` — clean slate for first visitor
- [ ] Side card with task #1 prompt visible to visitor
