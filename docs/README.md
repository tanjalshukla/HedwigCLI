# Hedwig landing site (R5)

Static one-page marketing site. Single self-contained `index.html` — no build
step, no framework, no backend, no analytics. Open it in a browser or serve the
folder statically.

```bash
# local preview
python -m http.server 8000 --directory site
# → http://localhost:8000
```

## Deploy — GitHub Pages (chosen)

Deployment is wired via `.github/workflows/pages.yml`, which uploads this
`site/` folder as the Pages artifact and publishes it. It runs on pushes that
touch `site/` (and can be triggered manually via `workflow_dispatch`).

**The workflow is INERT until you enable Pages — this is deliberate** (the site
has open publish-blockers; see below). To go live once they're cleared:

1. Repo **Settings → Pages → Build and deployment → Source: "GitHub Actions"**.
2. Push to `main` (or run the workflow manually from the Actions tab).
3. Site publishes to `https://tanjalshukla.github.io/HedwigCLI/`.

### Custom domain (later)

Ship on the `*.github.io` subdomain first. To add `hedwig.dev` (or similar)
once you own it: add the domain under Settings → Pages, drop a `CNAME` file in
`site/` containing the bare domain, and point DNS at GitHub Pages. **Confirm
availability before purchasing** — several other "Hedwig" projects exist.

### Why not Vercel

Considered and declined: Pages keeps everything in one repo with no third-party
service connected, free TLS, and config that lives beside the site. Vercel's
per-PR previews don't earn their keep for a single static page.

## Open placeholders (honesty rules §10)

The brief forbids invented numbers, install commands, or claims. One marker
remains in `index.html`:

1. **`{{DEMO_VIDEO}}`** (hero video frame) — the 90-second demo is
   founder-produced. Swap the placeholder frame for the real embed
   (`<iframe>` or `<video>`) when the asset lands. The site is publishable
   without it; the frame degrades to a labelled placeholder.

Resolved (no longer blockers): the headline-number block was reframed as an
honest "numbers come from your usage" promise (no pre-deployment figure is
fabricated), and the footer contact is the real address. There is no hard
prompt-reduction percentage anywhere — by design, since nothing is deployed yet.

## Publish gate (R5 / G2) — install verified

The install commands shown match `plugin/README.md` exactly:

```
claude plugin marketplace add tanjalshukla/HedwigCLI
claude plugin install hedwig@hedwig-marketplace
```

This flow was executed end-to-end against the real `claude` CLI — marketplace
add → install → the vendored governance core (including the online
logistic-regression classifier) ships in the installed copy — so R5's
"confirmed working before the site goes live" gate is satisfied for the install
path. Re-verify if the marketplace name or repo path changes. (The learned
scorer additionally needs `numpy`/`scikit-learn`/`fastembed` importable by the
hook interpreter; absent them the plugin degrades cleanly to the heuristic.)

## Honesty claims verified at build time

- **"Nothing phones home"** — grep of the entire `plugin/` tree for network
  calls (`requests`, `urllib`, `http.client`, `socket`, `httpx`, `boto`, etc.)
  found none on the core path. Re-verify before each publish, since the plugin
  is still under active development.
- No "AI-powered" / "reinforcement learning" / "neural" / "fully autonomous"
  language is used. The site describes honest heuristics + local outcome
  learning, matching the code and CLAUDE.md guardrails.

## Brand

- Owl ASCII mark and the cyan-information / green-learning palette are carried
  over from the CLI (`sc/run/banner.py`, `sc/run/theme.py`) so the site and the
  tool read as one product.
- Favicon is an inline 🦉 SVG data-URI (no asset file needed).
