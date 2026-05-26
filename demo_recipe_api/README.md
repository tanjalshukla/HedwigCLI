# Demo Recipe API

This directory contains the recipe CRUD backend used in the Hedwig booth demo at ACM CAIS 2026.

The fixture is intentionally incomplete. Visitors build features with Hedwig and see the results live in their browser.

## Running the demo

```bash
# Terminal 1: start the web app (visitors see this)
python server.py
# → open http://localhost:5001

# Terminal 2: start Hedwig
rm -f .sc/trust.db
AWS_PROFILE=dev hw
```

The browser auto-refreshes every 3 seconds. Any field added to the `Recipe` model appears on the cards immediately.

## Visitor features

See `FEATURES.md` for the full list of things visitors can build.

The fixture is intentionally incomplete. It provides a working baseline with the four core CRUD operations, then stops. Filtering, search, pagination, authentication enforcement, and several data fields are all left out — each one is a visitor feature.

## What's here

```
recipe_api/
  models.py    — Recipe (id, title, description, ingredients, tags, author),
                 Ingredient (name, amount), Tag
  service.py   — list_recipes, get_recipe, create_recipe, delete_recipe;
                 _validate_* helpers and _require_* existence checks
  api.py       — handler functions (_ok() wrapper, no web framework)
  auth.py      — stub auth layer; authenticate() exists but is a no-op
                 (enforcing it is a Tier 3 visitor feature)
tests/
  test_api.py  — baseline tests covering the four CRUD endpoints
docs/
  recipe_api_spec.md  — API surface, data model, and design constraints
                        (pass as --spec context during hw runs)
```

No web server is involved. The handler layer in `api.py` takes plain dicts and returns `(dict, int)` tuples, so every test runs without starting a server. This keeps the fixture fast and removes the need for a running process during the demo.

## Running the tests

From the repository root:

```bash
PYTHONPATH=demo_recipe_api .venv/bin/python -m pytest demo_recipe_api/tests -q
```

## Visitor features — three tiers

See `FEATURES.md` for the full laminated card. Summary:

**Tier 1 — Quick wins (~5 min).** Single-field additions and light validation changes. One or two files. Hedwig auto-approves after a few sessions; checks in cold. Good for showing the transition from heuristic to learned governance.

**Tier 2 — Scope-narrowing territory (~10 min).** Features that naturally span 2-3 files (service, api, models). The agent will over-reach; the visitor narrows scope. Three scope-constraint denials are enough to surface the hypothesis panel.

**Tier 3 — Governance-heavy (~15 min).** Security-sensitive changes, API surface rewrites, new dependencies. Hedwig always checks in regardless of calibration level. Good for the "why does it pause here?" conversation.

## Running the demo

See `DEMO_FLOW.md` for the operator's guided arc, exact task strings, what to approve and deny, and troubleshooting notes.

## Resetting between visitors

Reset governance state only (keep code):

```bash
rm -f .sc/trust.db
```

Reset both governance state and code:

```bash
rm -f .sc/trust.db
git restore recipe_api/
```

After the first 3+ visitors, consider keeping the DB to demonstrate pre-warmed calibration. See `DEMO_FLOW.md` for when to reset vs. keep.
