#!/usr/bin/env python3
"""Regenerate plugin/vendor/sc/ from the parent research repo.

The plugin must install and run *standalone* — `/plugin install` cannot
assume the 300-file research repo is sitting next to it. So we vendor the
slim Tier-0 import closure of the governance core into plugin/vendor/sc/
and make the plugin import from there.

This script is the single source of truth for *what* gets vendored. Run it
from the repo root whenever the vendored modules change during development:

    python plugin/sync_vendor.py

It copies an explicit allowlist (the verified Tier-0 closure), not a blind
tree copy — so a stray new module in sc/ doesn't silently bloat the plugin
or drag in a credentialed dependency. If a vendored module grows a new
import outside the allowlist, the verification step at the bottom catches
it.

CLOSURE (verified 2026-06-24 by tracing the LIVE import set of all plugin
bins — decide / record / verify / status):

  The default plugin closure (SQLite trace store + online log-reg classifier
  — both core novelty, NOT optional):
    sc/__init__.py
    sc/features.py
    sc/ml_policy.py
    sc/policy.py
    sc/retrieval.py
    sc/trust_db.py
    sc/store/__init__.py
    sc/store/types.py
    sc/store/{lease,rule,trace,pref,model}_store.py

sc/ml_policy.py IS vendored (RESTORED 2026-06-25 per S5). The earlier decision
to exclude it "to keep the plugin zero-dep" was reversed: the SQLite trace
store + online logistic-regression scorer is the contribution and must ship in
the default plugin. The zero-dep framing was already obsolete once R3
standardized `fastembed` (which pulls numpy transitively); given numpy is in,
scikit-learn's marginal cost is only scipy + joblib (no torch). model_store
loads/saves the PolicyClassifier; decide selects it via select_scorer() once
MIN_SAMPLES_FOR_LEARNED real decisions accumulate (heuristic carries
cold-start). The wall MOVED, it didn't vanish: numpy/sklearn/fastembed are
allowed on the decide path; torch/anthropic/boto are NOT (enforced by the
dependency-wall test). `test_ml_policy_is_vendored` confirms it's present.

sc/retrieval.py IS vendored (added with R3): rule_store.py imports it at
module scope (`from ..retrieval import ...`), and rule_store is in the live
closure (TrustDB inherits RuleStoreMixin). retrieval.py's top-level imports
are stdlib only (math, re) — `fastembed` is imported lazily inside
_build_fastembed_fn(); when fastembed isn't installed the EmbeddingRanker
silently degrades to KeywordRanker, exactly as the seam's fallback specifies.

sc/preferences.py and sc/session_state.py are likewise NOT in the live
closure today (the bins don't import them). When the deferred protocol work
wires session_state into a bin, add it here — the closure-violation check
below will demand it the moment a vendored module imports it at module scope.
"""

from __future__ import annotations

import ast
import shutil
import sys
from pathlib import Path


# Explicit allowlist of vendored modules, relative to the sc/ package root.
# This is exactly the LIVE import closure of the plugin bins — no inert weight.
# ml_policy.py IS included (RESTORED 2026-06-25 per S5; see module docstring):
# the online log-reg classifier is core novelty and ships in the default
# plugin. The dependency wall moved rather than vanished — numpy/sklearn/
# fastembed are sanctioned on the decide path; torch/anthropic/boto remain
# forbidden (NON_VENDORED_SC plus the closure check below enforce it).
VENDORED_MODULES: tuple[str, ...] = (
    "__init__.py",
    "features.py",
    "ml_policy.py",
    "policy.py",
    "preferences.py",
    "repo_memory.py",
    "retrieval.py",
    "trust_db.py",
    "store/__init__.py",
    "store/types.py",
    "store/lease_store.py",
    "store/rule_store.py",
    "store/trace_store.py",
    "store/pref_store.py",
    "store/model_store.py",
)

# sc submodules we deliberately DON'T vendor. If a vendored module imports
# one of these at module top level (not inside a function / TYPE_CHECKING),
# that's a closure violation and the script fails loudly. Lazy imports
# inside functions are fine — the Tier-0 decide path never calls them.
NON_VENDORED_SC = {
    "autonomy",
    "agent_client",
    "preference_inference",
    "hypothesis_bank",
    "regret",
    "cochange",
    "prompt_builder",
    "schema",
    "config",
    "run",
}


def _repo_root() -> Path:
    # plugin/sync_vendor.py -> plugin/ -> repo root
    return Path(__file__).resolve().parent.parent


def _vendor_root() -> Path:
    return Path(__file__).resolve().parent / "vendor" / "sc"


def _top_level_sc_imports(source: str) -> set[str]:
    """Return sc submodule names imported at module top level.

    Walks the AST and only considers imports at module scope (not nested in
    functions or `if TYPE_CHECKING:` blocks), since those are the only ones
    that run at import time on the decide path.
    """
    tree = ast.parse(source)
    found: set[str] = set()

    for node in tree.body:  # module-scope statements only
        if isinstance(node, ast.ImportFrom):
            # Relative import: node.module is the dotted path after the dots.
            # `from ..autonomy import X` -> level=2, module="autonomy"
            # `from .store.types import Y` -> level=1, module="store.types"
            if node.level and node.module:
                top = node.module.split(".")[0]
                found.add(top)
    return found


def main() -> int:
    repo_root = _repo_root()
    sc_root = repo_root / "sc"
    vendor_root = _vendor_root()

    if not sc_root.is_dir():
        print(f"error: sc/ not found at {sc_root}", file=sys.stderr)
        return 1

    # Wipe and recreate so deletions in the closure propagate.
    if vendor_root.exists():
        shutil.rmtree(vendor_root)
    vendor_root.mkdir(parents=True)

    violations: list[str] = []
    copied = 0

    for rel in VENDORED_MODULES:
        src = sc_root / rel
        if not src.is_file():
            print(f"error: closure file missing: {src}", file=sys.stderr)
            return 1
        dst = vendor_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        source = src.read_text()
        dst.write_text(source)
        copied += 1

        # Closure check: no top-level import of a non-vendored sc submodule.
        for top in _top_level_sc_imports(source):
            if top in NON_VENDORED_SC:
                violations.append(f"{rel}: top-level `from ..{top}` (not vendored)")

    # Marker so it's obvious these files are generated, not hand-edited.
    (vendor_root / "VENDORED.txt").write_text(
        "Generated by plugin/sync_vendor.py — do not edit by hand.\n"
        "Re-run `python plugin/sync_vendor.py` from the repo root to refresh.\n"
    )

    if violations:
        print("CLOSURE VIOLATION — vendored module imports a non-vendored sc submodule:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print("Fix: vendor the missing module, or make the import lazy/TYPE_CHECKING.", file=sys.stderr)
        return 2

    print(f"vendored {copied} modules into {vendor_root.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
