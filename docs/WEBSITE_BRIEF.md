# Hedwig — Website Build Brief

A self-contained spec for agents building the Hedwig landing site. Everything
needed about *what Hedwig is and how it works* is in here — you should not need
to read the source to write accurate copy. Where a number or claim is marked
**[MEASURED]**, it must come from a real run (see "Honesty rules"), not be
invented.

Companion docs (context, not required): `docs/POST_CAIS_MIGRATION.md` (the
plan; R5 is this site), `CONTEXT.md` (domain vocabulary), `CLAUDE.md` (project
guardrails).

---

## 1. Mission of the site

Two jobs, in priority order:

1. **Get the plugin installed.** Every visitor should be able to install the
   Hedwig Claude Code plugin in under a minute and understand the value in 30
   seconds. This is the user-acquisition flywheel: users → real traces →
   the learning loop improves.
2. **Make Hedwig look real and serious** to a technical visitor — an engineer,
   an advisor, or someone at a company like Databricks or Anthropic doing a
   quick look-up. Polished, honest, legible.

It is a **static one-page marketing site**, not a web app. No login, no
dashboard, no server-side telemetry, no account system. If you find yourself
adding a backend, stop — that is out of scope.

---

## 2. What Hedwig is (use this language)

**One-liner:**
> Hedwig is a governance layer for Claude Code that learns when to interrupt
> you — and when to stay out of your way.

**The slightly longer version:**
> Coding agents ask for permission on everything or nothing. Hedwig sits
> between Claude Code and you, and decides — per action — whether to auto-apply
> it or pause for your review. It calibrates that decision from what actually
> happened in your past sessions, not from rules you have to write.

**What it is NOT** (do not imply these):
- Not a coding agent. It does not write code. It governs the agent that does.
- Not a static allowlist or permission config. The decisions adapt.
- Not a cloud service. It runs locally; nothing phones home.
- Not a per-person profile. It learns per-repo and per-session (developer
  style isn't stable enough across sessions to personalize — this is a
  deliberate, defensible choice).

**The category, in one breath:** other tools let you *author* governance
policies; Hedwig *learns* them from your interaction outcomes.

---

## 3. How Hedwig works (the "how it works" section — accurate, plain)

Present this as a simple 3-step flow. This is the heart of the page.

**Step 1 — It scores every proposed edit.**
When Claude Code wants to edit, write, or patch a file, Hedwig assesses the
action's risk from raw signals: how big the change is, how many other files
depend on this one ("blast radius"), whether it's a security-sensitive file,
whether it's a brand-new file. No LLM call, no network — pure local scoring.

**Step 2 — It decides: auto-apply, or pause.**
Low-risk, familiar actions are applied silently — no permission prompt, you
stay in flow. Risky or unfamiliar ones surface for your review with a
plain-English reason ("touches a security-sensitive file and creates a new
file — surfacing this one"). The headline outcome: **[MEASURED] cut ~N% of
approval prompts while still surfacing the ones you'd want to review.**

**Step 3 — It learns from what happened.**
This is the part nothing else does. Every decision is recorded locally. When an
auto-applied edit gets reverted or fails verification, Hedwig registers a
"regret" and gets more cautious about similar actions next time. It corrects
its own over-trust. *No retraining, no synthetic data — it learns from real
outcomes.*

**The one sentence that captures it (use prominently, maybe as a pull-quote):**
> "Hedwig auto-approved this edit. You reverted it. Next time, it pauses on
> edits like that."

---

## 4. The pillars (a "features" section — 3 cards)

Keep to three. Depth over breadth.

1. **Learns from outcomes, not clicks.** Hedwig watches what survives and what
   gets undone, and recalibrates which interruptions are worth your attention.

2. **A real handshake, not just obedience.** The agent can flag its own
   uncertainty and ask to pause; Hedwig honors it. Governance goes both ways.

3. **Understands your rules by meaning.** Project guidelines are retrieved by
   semantic similarity, not keyword matching — a rule about "dependency
   injection" surfaces even when your task says "constructor arguments."

(Each maps to plan tasks R1/R2/R3 — but the site says nothing about internal
task names; describe the capability, not the project plumbing.)

---

## 5. Trust / honesty section (do not skip — it's a selling point)

A short "Runs entirely on your machine" block:
- All decision history is stored locally in your plugin data directory.
  **Nothing is sent to any server.** (This is literally true and must stay
  true — verify before publishing.)
- No credentials required for the core governance loop. Optional LLM features
  are opt-in with your own API key.
- Open about what it is: the scoring is honest heuristics plus local learning
  from your decisions — not a black box, not an overclaimed "AI brain."

This section is part of the acquisition-legibility goal: technical visitors
trust a tool that's precise about its own mechanism.

---

## 6. Install / download section

The primary call to action. Provide the Claude Code plugin install path.

- Show the install command(s) for the Hedwig Claude Code plugin. **Use the
  exact commands from the finalized `plugin/README.md` — do not invent install
  syntax.** If the README isn't final when you build, leave a clearly marked
  `{{INSTALL_COMMANDS}}` placeholder and flag it; do NOT guess.
- Link to the GitHub repo.
- State the requirement honestly: "Works with Claude Code. One install plus a
  small one-time model download (~30MB) — no GPU, no torch, no AWS."
- Secondary CTA: link to the 90-second demo video (embed it near the top too).

**Hard gate:** the install instructions on the site MUST be verified working on
a clean machine before the site goes live. A broken install from a LinkedIn
click is worse than no site. (This is QC's sign-off, per the plan's R5 gate.)

---

## 7. Page structure (recommended order)

1. Hero: one-liner + the pull-quote + primary "Install" CTA + embedded demo
   video.
2. The problem (2-3 lines): coding agents interrupt on everything or nothing.
3. How it works: the 3 steps from §3, ideally with a simple diagram or
   animation of the score → decide → learn loop.
4. The three pillars (§4 cards).
5. The headline number (§3 step 2), stated once, big. **[MEASURED]**
6. Runs-on-your-machine / trust section (§5).
7. Install + GitHub + video (§6).
8. Footer: GitHub, contact, "from the team behind the ACM CAIS 2026 best-demo
   Hedwig" (credibility, one line).

---

## 8. Design direction

- **Tone:** confident, technical, honest. Engineers are the audience. No hype
  words, no "revolutionary," no fake metrics.
- **Aesthetic:** clean, modern developer-tool landing page (think Linear /
  Vercel / a polished OSS project). Dark or light, your call — legible and
  fast. The existing project has an owl/"Hedwig" sprite motif; a subtle owl
  mark is on-brand, but keep it tasteful, not cartoonish.
- **The diagram matters more than decoration.** The score → decide → learn loop
  is the product. A clear visual of it is worth more than any hero graphic.
- **Performance:** static, fast, no heavy frameworks needed. Plain HTML/CSS +
  minimal JS, or a static-site generator / single-component React if the team
  prefers — but the output must be a static deploy.

---

## 9. Tech & deployment

- Static site. GitHub Pages or Vercel. Custom domain (e.g. `hedwig.dev` or
  similar — confirm availability with the user).
- The 90-second demo video is produced separately (plan task S4) — embed it;
  don't block the site on producing it (use a placeholder if it lands late).
- No analytics that compromise the "nothing phones home" claim about the
  *plugin*. (Privacy-respecting site-level analytics like Plausible are fine
  and don't contradict the plugin claim, but if in doubt, ask the user.)

---

## 10. Honesty rules (non-negotiable — this protects credibility and acquisition DD)

- **No invented numbers.** Any percentage or metric must come from a real
  measured run. If you don't have it yet, use `{{HEADLINE_NUMBER}}` and flag it.
- **No overclaim.** Do not say "AI-powered," "reinforcement learning,"
  "neural," or "fully autonomous." Hedwig is honest heuristics + local outcome
  learning. Describe exactly that. An engineer who reads the code after
  visiting the site must find it matches.
- **The privacy claim must be true.** "Nothing phones home" is only printable
  if the shipped plugin actually makes no network calls on the core path.
  Confirm before publishing.
- **No invented install commands.** Pull from the real README or use a
  placeholder.

When unsure whether a claim is defensible, leave a `{{TODO: verify with user}}`
marker rather than shipping a guess.

---

## 11. What success looks like

A stranger lands from a LinkedIn post, understands in 30 seconds what Hedwig
does and why it's different, installs it in under a minute, and it works on the
first try. A Databricks/Anthropic engineer lands, sees a precise honest
description of a learned governance layer, and thinks "this is the thing
Omnigent's policy socket is missing." Both walk away trusting it.
