#!/usr/bin/env python3
"""Hedwig hypothesis confirmation surface (the /hedwig-learn command).

The hypothesis bank watches how you work and, when a pattern accumulates enough
evidence, marks a candidate `ready_to_surface`. In the research CLI the REPL
asks "confirm? [y/n]" inline. The plugin can't — Claude Code hooks are
non-interactive — so confirmation is this slash command:

    /hedwig-learn            -> show the pattern waiting for review (if any)
    /hedwig-learn confirm    -> accept it; it becomes an active preference that
                                tightens future decisions in the cascade
    /hedwig-learn reject     -> decline it; stays in the bank as declined,
                                nothing silently discarded

Confirmed preferences are persisted exactly as the CLI persists them
(save_confirmed_preference with the {accepted, driver, preference} envelope) so
the decide hook's preference-application step (hedwig-decide) consumes them
unchanged. Persist-before-mark ordering matches the CLI: a candidate is only
marked surfaced once its backing preference row is saved, so it can never strand
as confirmed-with-no-preference. Always exits 0; local, no credentials.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _hedwig_common import open_trust_db, repo_root_key  # noqa: E402

# The plugin keys repo history on cwd (same as the other hooks); the slash
# command runs in the project dir, and the session is the current one. We
# surface repo-scoped ready candidates so a pattern that crossed threshold in a
# prior session is still confirmable now (mirrors the CLI's repo scoping).
_SESSION = os.environ.get("CLAUDE_SESSION_ID", "") or "plugin"


def _ready(db):
    """The highest-priority ready candidate for this repo, or None."""
    from sc.hypothesis_bank import get_ready_hypothesis  # noqa: PLC0415

    return get_ready_hypothesis(
        trust_db=db, repo_root=repo_root_key(None), session_id=_SESSION
    )


def _cmd_show() -> int:
    try:
        db = open_trust_db()
        hyp = _ready(db)
    except Exception as exc:
        sys.stdout.write(f"Could not read the hypothesis bank: {exc}\n")
        return 0
    if hyp is None:
        sys.stdout.write(
            "No pattern is waiting for review right now.\n"
            "Hedwig surfaces one once it has enough evidence from how you work.\n"
        )
        return 0
    sys.stdout.write(
        "Hedwig noticed a pattern:\n\n"
        f"  {hyp.prompt}\n\n"
        f"  Why: {hyp.rationale}\n\n"
        "Confirm it with  /hedwig-learn confirm  — it'll then tighten how Hedwig\n"
        "governs similar edits. Decline with  /hedwig-learn reject .\n"
    )
    return 0


def _resolve(confirmed: bool) -> int:
    try:
        from sc.hypothesis_bank import mark_candidate_surfaced  # noqa: PLC0415
        from sc.preferences import preference_to_dict  # noqa: PLC0415

        db = open_trust_db()
        hyp = _ready(db)
        if hyp is None:
            sys.stdout.write("Nothing is waiting for review — nothing to confirm or decline.\n")
            return 0
        repo = repo_root_key(None)
        if confirmed:
            payload = {
                "accepted": True,
                "driver": hyp.driver,
                "preference": preference_to_dict(hyp.proposed_preference),
            }
        else:
            payload = {"accepted": False, "driver": hyp.driver}
        # Persist the preference first; only mark surfaced if the save succeeds,
        # else the candidate would strand as confirmed with no backing row.
        db.save_confirmed_preference(
            repo_root=repo,
            session_id=_SESSION,
            preference_json=json.dumps(payload),
            driver=hyp.driver,
        )
        mark_candidate_surfaced(
            trust_db=db,
            repo_root=repo,
            session_id=_SESSION,
            driver=hyp.driver,
            confirmed=confirmed,
        )
    except Exception as exc:
        sys.stdout.write(f"Could not record your choice: {exc}\n")
        return 0
    if confirmed:
        sys.stdout.write(
            f"Confirmed. Hedwig will apply this from now on: {hyp.prompt}\n"
        )
    else:
        sys.stdout.write(
            "Declined. Hedwig won't apply it; it stays in the bank for transparency.\n"
        )
    return 0


def main(argv: list[str]) -> int:
    verb = (argv[0].lower() if argv else "show")
    if verb in ("confirm", "accept", "yes", "y"):
        return _resolve(confirmed=True)
    if verb in ("reject", "decline", "no", "n"):
        return _resolve(confirmed=False)
    return _cmd_show()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
