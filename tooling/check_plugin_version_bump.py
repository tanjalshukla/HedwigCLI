#!/usr/bin/env python3
"""Fail if plugin code changed without a version bump.

Users install the plugin pinned to a git SHA and only pull changes when the
version in plugin/.claude-plugin/plugin.json increases (`claude plugin update`
surfaces the new version). So any change under plugin/ that ships behavior must
come with a version bump — otherwise users silently never receive it.

This check compares the working changes against a git base ref:
  - if any tracked file under plugin/ changed, AND
  - the version string in plugin/.claude-plugin/plugin.json did NOT increase,
  then exit 1 with an explanation.

Vendor-only churn still counts: plugin/vendor/ is what actually runs on the
user's machine, so a vendored-code change is a user-facing change.

Usage:
    python3 tooling/check_plugin_version_bump.py [BASE_REF]

BASE_REF defaults to origin/main. In CI, pass the PR base or the push's
before-SHA. Runs git under the hood; must be invoked inside the repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = "plugin/.claude-plugin/plugin.json"
PLUGIN_PREFIX = "plugin/"


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _parse_version(text: str) -> tuple[int, ...]:
    """Parse a dotted semver string into a comparable tuple. Non-numeric
    suffixes (e.g. -beta) are dropped for the comparison."""
    raw = json.loads(text).get("version", "")
    parts = []
    for chunk in str(raw).split("."):
        num = "".join(c for c in chunk if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _file_at_ref(ref: str, path: str) -> str | None:
    """Return file contents at a git ref, or None if it didn't exist there."""
    try:
        return _git("show", f"{ref}:{path}")
    except RuntimeError:
        return None


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"

    # Files under plugin/ that changed vs. the base ref.
    try:
        diff = _git("diff", "--name-only", base, "--", PLUGIN_PREFIX)
    except RuntimeError as exc:
        print(f"[version-check] could not diff against {base}: {exc}", file=sys.stderr)
        print("[version-check] skipping (base ref unavailable)", file=sys.stderr)
        return 0

    changed = [line for line in diff.splitlines() if line.strip()]
    if not changed:
        print(f"[version-check] no plugin/ changes vs {base} — nothing to gate.")
        return 0

    # Current (working tree) version vs. version at the base ref.
    current_text = (REPO_ROOT / PLUGIN_MANIFEST).read_text()
    current_version = _parse_version(current_text)

    base_text = _file_at_ref(base, PLUGIN_MANIFEST)
    base_version = _parse_version(base_text) if base_text else (0,)

    if current_version > base_version:
        cur = ".".join(map(str, current_version))
        old = ".".join(map(str, base_version))
        print(f"[version-check] OK — plugin version bumped {old} → {cur}.")
        return 0

    cur = ".".join(map(str, current_version))
    print(
        f"[version-check] FAIL — {len(changed)} file(s) under plugin/ changed but "
        f"the version in {PLUGIN_MANIFEST} is still {cur}.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("Changed plugin files:", file=sys.stderr)
    for f in changed:
        print(f"  {f}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Users only receive plugin changes when this version increases. "
        f"Bump the version in {PLUGIN_MANIFEST} (and run `make sync-vendor` "
        "if you edited sc/) before merging.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
