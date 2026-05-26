from __future__ import annotations

"""Translate PolicyDecision.reasons codes into one plain-English sentence.

The reasons strings (see sc/policy.py) are structured for trace analysis:
"-risk:new file", "+history:deliberate review pace", etc. They're useful
for observability but unreadable at check-in time. This module picks the
single most important reason per check-in and renders it as a sentence the
developer can act on.

Intentionally terse — one line, no jargon, no list. The full breakdown is
always available in `hw observe traces`.
"""

from collections import Counter

from ..policy import PolicyDecision


# Reason-code → human sentence. Keys are the prefix up to the colon.
# Order in the priority list below determines which reason wins if a
# decision has several.
_REASON_SENTENCES: dict[str, str] = {
    "hard constraint: always_check_in": "a hard constraint requires a check-in here",
    "hard constraint: always_deny": "a hard constraint blocks this path",
    "confirmed preference forced check-in": "a preference you confirmed earlier asked me to check in on changes like this",
    "failure-signal trigger: debug intent + prior failure this session": "something failed earlier in this debug session, so I want a closer look",
    "-risk:security sensitive": "this touches security-sensitive code",
    "-risk:large diff": "the change is large enough to be worth a second look",
    "-risk:multi-file blast radius": "many other files import this one, so changes here ripple",
    "-risk:large multi-file action": "this is a large multi-file change",
    "-risk:new file": "a new file is being introduced",
    "-risk:interface change": "this looks like an interface/API change",
    "-risk:config change": "this is a configuration change",
    "-risk:dependency update": "this updates project dependencies",
    "-risk:medium diff": "the change is non-trivial in size",
    "-risk:multi-file action": "this spans multiple files",
    "-session:recent denials": "you've denied similar changes recently",
    "-history:denials": "you've declined similar writes here before",
    "-quality:edit distance": "past edits here often needed rework",
    "soft-checkin trigger matched": "a soft-confirm preference matched; flagging before I proceed",
    "adaptive policy disabled": "adaptive policy is off, so I default to checking in",
}


# Priority order — whichever reason lands first wins the sentence.
_PRIORITY: tuple[str, ...] = (
    "hard constraint: always_deny",
    "hard constraint: always_check_in",
    "confirmed preference forced check-in",
    "failure-signal trigger: debug intent + prior failure this session",
    "-risk:security sensitive",
    "-risk:large diff",
    "-risk:large multi-file action",
    "-risk:multi-file blast radius",
    "-risk:interface change",
    "-risk:config change",
    "-risk:dependency update",
    "-risk:new file",
    "-risk:medium diff",
    "-risk:multi-file action",
    "-session:recent denials",
    "-history:denials",
    "-quality:edit distance",
    "soft-checkin trigger matched",
    "adaptive policy disabled",
)


def _match_prefix(reason: str) -> str | None:
    for key in _REASON_SENTENCES:
        if reason.startswith(key):
            return key
    return None


def synthesize_pause_reason(
    policies: dict[str, PolicyDecision],
    check_in_files: list[str],
) -> str | None:
    """Return one short sentence summarizing why Hedwig is pausing.

    Picks the highest-priority reason across the check-in files. Returns
    None when no recognized reason is present (in which case the UI
    simply omits the explanation line).
    """
    seen: Counter[str] = Counter()
    for path in check_in_files:
        decision = policies.get(path)
        if decision is None:
            continue
        for reason in decision.reasons:
            key = _match_prefix(reason)
            if key is not None:
                seen[key] += 1

    if not seen:
        return None

    for key in _PRIORITY:
        if key in seen:
            sentence = _REASON_SENTENCES[key]
            n = seen[key]
            if n > 1 and len(check_in_files) > 1:
                sentence = sentence + f" (applies to {n} of these files)"
            return sentence
    return None
