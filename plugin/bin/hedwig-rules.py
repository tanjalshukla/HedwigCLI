#!/usr/bin/env python3
"""Hedwig hard-constraint authoring + listing (the /hedwig-rules surface).

Hard constraints are the deterministic, non-negotiable layer of the cascade:
the plugin's decide hook resolves them BEFORE the scorer (see
hedwig-decide._constraint_decision), so a developer-set rule like
"never auto-touch config/prod/**" blocks the edit outright. This script is the
plugin's way to create and inspect those rules — the CLI's `hw rules` analogue.

Scope: PATH-PATTERN constraints only (the deterministic, enforceable case).
A path pattern + a policy is all the enforcement layer needs — no model call,
so this runs locally with no credentials. Natural-language rule classification
("never touch prod" -> pattern + policy) is the LLM-assisted path and is a
separate, opt-in surface; this command takes the pattern explicitly.

Usage (invoked by the /hedwig-rules slash command, args forwarded):
    hedwig-rules.py list
    hedwig-rules.py add <deny|check_in|allow> <path-glob>
    hedwig-rules.py remove <path-glob>

Always exits 0 with a human-readable line on stdout; a storage failure reports
the error rather than raising, so the slash command never wedges.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _hedwig_common import open_trust_db, owl_str, repo_root_key  # noqa: E402

# The three policies a developer can set, mapped to the stored constraint_type
# the cascade reads (HardConstraint.policy_for -> always_*). Short aliases on
# the left so the command line stays terse.
_POLICY_ALIASES = {
    "deny": "always_deny",
    "check_in": "always_check_in",
    "checkin": "always_check_in",
    "allow": "always_allow",
}
_SOURCE = "plugin_user"  # provenance tag: authored via /hedwig-rules


def _repo_root() -> str:
    """The repo the rules apply to. The slash command runs in the project cwd;
    canonicalized through repo_root_key so it matches the key the event hooks
    derive from payload["cwd"] (else a rule added here keys differently than
    hedwig-decide reads it and silently never fires)."""
    return repo_root_key(None)


def _cmd_list() -> int:
    try:
        db = open_trust_db()
        constraints = db.list_constraints(_repo_root())
    except Exception as exc:
        sys.stdout.write(f"Could not read constraints: {exc}\n")
        return 0
    if not constraints:
        sys.stdout.write(
            f"{owl_str()}\n\n"
            "No hard constraints set for this repo.\n"
            "Add one with: /hedwig-rules add deny <path-glob>\n"
        )
        return 0
    sys.stdout.write(f"{owl_str()}\n\nHard constraints for this repo:\n")
    for c in constraints:
        write_policy = c.policy_for("write")
        sys.stdout.write(f"  {write_policy:<16} {c.path_pattern}\n")
    return 0


def _cmd_add(policy_alias: str, pattern: str) -> int:
    policy = _POLICY_ALIASES.get(policy_alias.lower())
    if policy is None:
        sys.stdout.write(
            f"Unknown policy '{policy_alias}'. Use one of: deny, check_in, allow.\n"
        )
        return 0
    if not pattern.strip():
        sys.stdout.write("A path pattern is required, e.g. config/prod/**\n")
        return 0
    try:
        from sc.trust_db import HardConstraint  # noqa: PLC0415

        db = open_trust_db()
        constraint = HardConstraint.for_both(
            path_pattern=pattern.strip(),
            constraint_type=policy,
            source=_SOURCE,
            overridable=False,
        )
        added = db.add_constraints(_repo_root(), [constraint])
    except Exception as exc:
        sys.stdout.write(f"Could not add constraint: {exc}\n")
        return 0
    if added:
        sys.stdout.write(f"Added: {policy} for {pattern.strip()}\n")
    else:
        sys.stdout.write(f"Already set: {policy} for {pattern.strip()}\n")
    return 0


def _cmd_remove(pattern: str) -> int:
    if not pattern.strip():
        sys.stdout.write("A path pattern is required to remove a constraint.\n")
        return 0
    try:
        db = open_trust_db()
        removed = db.delete_constraints(_repo_root(), path_pattern=pattern.strip())
    except Exception as exc:
        sys.stdout.write(f"Could not remove constraint: {exc}\n")
        return 0
    sys.stdout.write(
        f"Removed {removed} constraint(s) for {pattern.strip()}.\n"
        if removed
        else f"No constraint matched {pattern.strip()}.\n"
    )
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "list":
        return _cmd_list()
    verb = argv[0]
    if verb == "add" and len(argv) >= 3:
        return _cmd_add(argv[1], " ".join(argv[2:]))
    if verb == "remove" and len(argv) >= 2:
        return _cmd_remove(" ".join(argv[1:]))
    sys.stdout.write(
        "Usage:\n"
        "  /hedwig-rules list\n"
        "  /hedwig-rules add <deny|check_in|allow> <path-glob>\n"
        "  /hedwig-rules remove <path-glob>\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
