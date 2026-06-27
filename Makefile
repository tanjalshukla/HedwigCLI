# Hedwig — development harness.
#
# One verify loop that both humans and agents run before calling work done.
# CLAUDE.md §4 (goal-driven execution) points here; CI runs `make verify`.
#
#   make install      editable install + dev deps into .venv
#   make test         full test suite
#   make lint         ruff (undefined names, unused imports, syntax)
#   make verify       lint + test + vendor-sync check  ← the done-criterion
#   make sync-vendor  regenerate plugin/vendor/sc from sc/
#   make demo-test    run the demo-fixture suite (separate PYTHONPATH)
#   make clean        remove caches and build artifacts

# Prefer the project venv if present; fall back to whatever python3 is on PATH
# (CI installs into the ambient interpreter).
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
PYTEST := PYTHONPATH=. $(PY) -m pytest

.DEFAULT_GOAL := help
.PHONY: help install test lint verify sync-vendor vendor-check demo-test clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?# .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?# "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: # editable install + dev deps into .venv
	$(PY) -m pip install --upgrade pip setuptools wheel
	$(PY) -m pip install --no-build-isolation -e .
	$(PY) -m pip install pytest ruff

test: # full test suite
	$(PYTEST) tests -q

lint: # ruff — undefined names (F8), unused imports (F4), syntax (F9)
	$(PY) -m ruff check sc/ plugin/ tests/

# The single loop. Lint first (fast fail), then the suite, then prove the
# plugin's vendored copy of sc/ is in sync — a stale vendor ships bugs the
# tests never see.
verify: lint test vendor-check # lint + test + vendor-sync check
	@echo "✓ verify: lint + tests + vendor all green"

sync-vendor: # regenerate plugin/vendor/sc from sc/
	$(PY) plugin/sync_vendor.py

# Regenerate into a temp check: run the sync, then fail if it changed anything
# tracked. Keeps "edited sc/ but forgot to re-vendor" out of main.
vendor-check: # fail if plugin/vendor is out of sync with sc/
	@$(PY) plugin/sync_vendor.py >/dev/null
	@if ! git diff --quiet -- plugin/vendor/; then \
		echo "✗ plugin/vendor is out of sync with sc/ — run 'make sync-vendor' and commit."; \
		git --no-pager diff --stat -- plugin/vendor/; \
		exit 1; \
	fi
	@echo "✓ plugin/vendor in sync with sc/"

demo-test: # run the demo-fixture suite (separate PYTHONPATH)
	PYTHONPATH=demo_recipe_api $(PY) -m pytest demo_recipe_api/tests -q

clean: # remove caches and build artifacts
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
