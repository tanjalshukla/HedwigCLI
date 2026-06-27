from __future__ import annotations

"""Repo-memory synthesis — the "what we've learned about this repo" lead.

Single home for turning a repo's confirmed preferences + logic notes + recent
feedback into one human-readable paragraph. Both front-ends import it so the
text never drifts:

  * the research CLI injects it at the top of the system prompt
    (prompt_builder.build_run_system_prompt) and shows it in `/context`;
  * the Claude Code plugin injects it via the SessionStart hook's
    additionalContext so the model opens each session oriented.

Pure string templating over already-retrieved data — NO Bedrock call, no new
query path, stdlib + the trust DB only. That is what makes it safe to run on
the plugin's zero-credential decide-adjacent path.
"""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class LearnedPreference:
    """One learned preference, with a short why."""

    headline: str       # "I'll check in before multi-file changes"
    basis: str          # "You narrowed scope 3 times this session."
    scope: str          # "this session" | "this repo" | "everywhere"


# Driver → (headline, basis). The only drivers Hedwig emits today; a confirmed
# hypothesis carries one of these as its `driver`. Kept here so both the CLI
# status panel and the plugin's repo-memory paragraph humanize them identically.
_DRIVER_MAP: dict[str, tuple[str, str]] = {
    "scope_constraint": (
        "I'll pause before adding test files in the same change as service code",
        "You narrowed scope when tests were bundled with service changes.",
    ),
    "positive_redirect": (
        "I'll soft-check-in on small follow-ups",
        "You've been accepting quick small changes — I'll surface them without blocking.",
    ),
    "failure_reactive": (
        "I'll check in on non-trivial changes while things are unstable",
        "We've hit failures this session — I'm tightening oversight on larger edits until it stabilizes.",
    ),
    "deliberate_reviewer": (
        "I'll use soft check-ins on small diffs, full prompts for bigger ones",
        "You've been reviewing carefully — I'll save the full pause for changes that need your attention.",
    ),
    "rapid_approver": (
        "I'll always check in on larger changes",
        "You've been approving quickly — I'll make sure you stay in the loop on the bigger stuff.",
    ),
    "soft_checkin_tests": (
        "I'll surface a brief countdown panel before writes to test files",
        "You confirmed a soft pause for test-file changes so you stay in the loop without full interruption.",
    ),
}


def humanize_preference(payload: dict, *, scope: str) -> LearnedPreference | None:
    """Turn a persisted confirmed-preference payload into a human-readable
    LearnedPreference. Returns None for non-accepted payloads."""
    if not payload.get("accepted"):
        return None
    pref_dict = payload.get("preference")
    driver = payload.get("driver", "")

    if driver in _DRIVER_MAP:
        headline, basis = _DRIVER_MAP[driver]
        return LearnedPreference(headline=headline, basis=basis, scope=scope)
    # Fallback — if we can deserialize the preference, use the driver name.
    if pref_dict:
        try:
            from .preferences import preference_from_dict  # noqa: PLC0415

            preference_from_dict(pref_dict)
        except Exception:
            return None
        return LearnedPreference(
            headline="Adjusted check-in behavior",
            basis=f"Confirmed via hypothesis: {driver}.",
            scope=scope,
        )
    return None


def synthesize_repo_summary(
    *,
    trust_db,
    repo_root: str,
    logic_note_lines: list[str],
    feedback_snippets: list[str],
) -> str:
    """One-paragraph "what we've learned about this repo" lead.

    Pure string templating over already-retrieved data — no Bedrock call, no new
    query path. Returns "" when there's nothing meaningful to say.
    """
    fragments: list[str] = []

    pref_rows = trust_db.confirmed_preferences_for_repo(repo_root)
    pref_headlines: list[str] = []
    for r in pref_rows:
        try:
            payload = json.loads(r["preference_json"])
        except Exception:
            continue
        learned = humanize_preference(payload, scope="this repo")
        if learned and learned.headline:
            pref_headlines.append(learned.headline.rstrip("."))
        if len(pref_headlines) >= 3:
            break
    if pref_headlines:
        fragments.append("Confirmed preferences: " + "; ".join(pref_headlines) + ".")

    notes = [n.strip().rstrip(".") for n in logic_note_lines if n and n.strip()][:2]
    if notes:
        fragments.append("Repo facts: " + "; ".join(notes) + ".")

    fb = [f.strip().rstrip(".") for f in feedback_snippets if f and f.strip()][:1]
    if fb:
        fragments.append("Recent developer feedback: " + fb[0] + ".")

    return " ".join(fragments)
