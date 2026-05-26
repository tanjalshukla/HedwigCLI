# Build with Hedwig 🦔

**This recipe app grows over the course of the conference.** Every visitor adds something — a field, a filter, a new endpoint — and the next visitor builds on top of it. By day 3 the app should look very different from day 1.

Type any of these into `hedwig>`, or your own idea — then watch the browser update at **localhost:5001**.

*These are starting points, not a fixed menu. Whatever you build stays in the app for the next person. Hedwig governs each change: it'll ask before touching files, show you the diff, and learn from your approvals and pushback.*

---

## ✦ Quick wins (~5 min each)

- [ ] Add a `prep_time_minutes` field to recipes
- [ ] Add a `rating` field (1–5 stars)
- [ ] Add a `servings` field
- [ ] Add a `cuisine` field (e.g. Italian, Mexican, Japanese)
- [ ] Add a `difficulty` field (easy / medium / hard)
- [ ] Show the total ingredient count on each recipe card

---

## ✦ Filtering & search (~10 min)

- [ ] Filter recipes by tag — `GET /recipes?tag=vegetarian`
- [ ] Filter by cuisine — `GET /recipes?cuisine=italian`
- [ ] Add pagination — `GET /recipes?page=1&page_size=2`
- [ ] Search recipes by title keyword
- [ ] Sort by rating (highest first)

---

## ✦ New features (~15 min)

- [ ] Add a `favorites` endpoint — mark a recipe as saved
- [ ] Add a `published` / `draft` state — only published recipes show in the browser
- [ ] Add a `notes` field for cooking tips
- [ ] Add an ingredient substitution endpoint
- [ ] Let recipes have a `source_url` link

---

## ✦ Governance-heavy (always triggers a check-in)

- [ ] Enforce the API key in `auth.py` — requests without a token get 401
- [ ] Add input validation — reject recipes with no ingredients
- [ ] Add rate limiting
- [ ] Add a `DELETE /recipes/all` admin endpoint

---

## How it works

1. Type a feature into `hedwig>`
2. Hedwig shows you what it plans to change — **approve or push back**
3. If you narrow scope 3 times ("just service.py, not the tests"), Hedwig notices the pattern and asks if you want it as a standing preference
4. Approved changes appear in the browser automatically
5. Run `/prefs` to see what Hedwig has learned · `/observe weights` to see how it's adapted

---

*Built with Hedwig · ACM CAIS 2026 · github.com/tanjalshukla/HedwigCLI*
