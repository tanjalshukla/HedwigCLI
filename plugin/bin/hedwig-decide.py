#!/usr/bin/env python3
"""Hedwig PreToolUse adapter.

Reads the Claude Code PreToolUse payload from stdin, computes RiskSignals
via sc.features.assess_risk, scores via sc.policy.HeuristicScorer, and
emits a hookSpecificOutput JSON object on stdout that either:

  * suppresses the native permission prompt (`permissionDecision: "allow"`)
    when the scorer says "proceed" — the action is silently auto-applied
  * passes through (no decision, exit 0 silent) when the scorer says
    "check_in" — Claude Code's native prompt fires and the developer
    decides
  * blocks (`permissionDecision: "deny"` with reason fed back to the
    agent) when a hard constraint matched

This is the Tier-0 entry point: zero credentials required. The cascade runs
here, in order: (1) hard constraints (_constraint_decision — always_deny /
always_check_in / always_allow override everything), (2) per-file history +
the heuristic / learned scorer, (3) confirmed-preference application
(apply_confirmed_preferences — tighten by default; one narrow auto_apply
loosening exception), (4) the confidence handshake (tighten-only), (5a) the
deterministic security floor (a security-sensitive proceed is forced to
surface — invariant 5), (5b) the R6 deny gate. The repo memory layer
(SessionStart / UserPromptSubmit) and the hypothesis bank's
generate/surface/confirm loop live in the record / verify / learn hooks. See
the capability table in the root README for the full CLI-vs-plugin split.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Import the governance core from the vendored copy bundled with the plugin
# (plugin/vendor/sc), NOT the parent research repo. This is what makes the
# plugin installable standalone — `/plugin install` ships vendor/ and needs
# nothing else on disk. Regenerate vendor/ with `python plugin/sync_vendor.py`.
_HERE = Path(__file__).resolve().parent
_VENDOR = _HERE.parent / "vendor"  # plugin/bin/ -> plugin/ -> plugin/vendor
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from sc.features import assess_risk  # noqa: E402
from sc.policy import PolicyInput  # noqa: E402
from sc.trust_db import PolicyHistory  # noqa: E402

# Sibling helper in plugin/bin/ — ensure bin/ is importable regardless of cwd.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _hedwig_common import (  # noqa: E402
    DECISIONS_LOG,
    DENIED_VERDICT,
    LOW_CONFIDENCE_THRESHOLD,
    MAX_DENY_RETRIES,
    append_jsonl,
    apply_confirmed_preferences,
    ensure_learned_interpreter,
    latest_self_checkin,
    load_classifier,
    open_trust_db,
    prior_deny_count,
    repo_root_key,
    select_active_scorer,
)

# Tools we govern. Everything else passes through untouched.
_GOVERNED_TOOLS = {"Edit", "Write", "MultiEdit"}


def _emit(obj: dict) -> None:
    """Write a hookSpecificOutput JSON to stdout. Single emit per call."""
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def _plain_reason(*, verdict: str, rel: str, risk, history, is_new_file: bool) -> str:
    """Plain-English judgment for the developer at the decision moment.

    This string is Hedwig's only in-flow voice — it replaces the CLI's whole
    check-in panel. It must read like a colleague's one-line rationale, not a
    debug dump. Lead with the strongest reason; surface the regret money-shot
    ("you reverted a similar edit") first when prior outcome history is
    negative, since that's the most trust-building thing Hedwig can say.
    """
    name = rel.rsplit("/", 1)[-1]
    surfacing = verdict == "check_in"

    # The money-shot: prior bad outcome on this file. Most compelling reason.
    if history.denials > 0 and surfacing:
        return (
            f"You reverted or a check failed on a recent edit to {name} — "
            f"surfacing this one so you can take a look."
        )

    bits: list[str] = []
    if risk.is_security_sensitive:
        bits.append("touches security-sensitive code")
    if risk.blast_radius > 3:
        bits.append(f"{risk.blast_radius} files depend on it")
    if is_new_file:
        bits.append("creates a new file")
    if risk.diff_size > 80:
        bits.append("a large change")

    if surfacing:
        if bits:
            return f"{name}: " + _join(bits) + " — surfacing this one for your review."
        return f"{name}: outside what I've seen go well here — surfacing for your review."

    # Auto-applied (proceed / proceed_flag).
    if history.approvals > 0:
        return f"{name}: similar edits here have gone well — applying automatically."
    pattern = (risk.change_pattern or "general").replace("_", " ")
    return f"{name}: low-risk {pattern} — applying automatically."


def _join(items: list[str]) -> str:
    """Oxford-style join: ['a'] -> 'a'; ['a','b'] -> 'a and b'; ['a','b','c'] -> 'a, b, and c'."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _log_decision(
    payload: dict,
    file_path: str,
    verdict: str,
    score: float,
    risk,
    reason: str = "",
    *,
    edit_old: str = "",
    edit_new: str = "",
    scorer: str = "",
) -> None:
    """Append a decision event for /hedwig-status to tally and narrate.

    verdict is "suppressed" (auto-applied, no prompt) or "surfaced" (fell
    through to the native prompt for developer review). The plain-English
    reason rides along so the dashboard can show *why* a given edit was
    surfaced — the in-flow channel can't carry it on a passthrough today.
    One row per governed PreToolUse.

    edit_old/edit_new (Edit only) carry the substituted strings so the
    PostToolUse recorder can recognize a *later* edit that undoes an
    auto-applied one — the verification-independent regret signal (R1). Only
    written when non-empty so non-Edit rows stay lean.

    scorer ("heuristic"|"learned") records which adapter fired (S5), so
    longitudinal analysis can separate cold-start from learned-era decisions —
    the plugin analogue of decision_traces.policy_reasons.
    """
    row = {
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "file_path": file_path,
        "verdict": verdict,
        "score": round(float(score), 3),
        "change_pattern": risk.change_pattern,
        # Full risk signals so the PostToolUse recorder can reconstruct the
        # exact PolicyInput this decision scored on and replay it as a positive
        # classifier update — the only way sample_count grows on the plugin
        # path, so the learned scorer can actually reach ready() at the booth.
        "blast_radius": risk.blast_radius,
        "is_new_file": risk.is_new_file,
        "is_security_sensitive": risk.is_security_sensitive,
        "diff_size": risk.diff_size,
        "reason": reason,
        "scorer": scorer,
    }
    if edit_old or edit_new:
        row["edit_old"] = edit_old
        row["edit_new"] = edit_new
    append_jsonl(DECISIONS_LOG, row)


def _passthrough() -> int:
    """Exit 0 with no output → no decision → native permission flow runs."""
    return 0


def _empty_history() -> PolicyHistory:
    """Cold-start history when the DB is unavailable or the file is unseen."""
    return PolicyHistory(
        approvals=0,
        denials=0,
        effective_approvals=0.0,
        rubber_stamp_approvals=0,
        avg_response_ms=None,
        avg_edit_distance=0.0,
    )


def _open_db_safe():
    """Open the trust.db, or None on failure. Decide never blocks an edit on a
    DB hiccup — a None db degrades cleanly to cold-start history + the bare
    heuristic scorer."""
    try:
        return open_trust_db()
    except Exception:
        return None


def _security_paths_safe(db, repo_root: str) -> frozenset[str]:
    """Agent-flagged security paths for this repo, or an empty set on any failure.

    Read on every decide; a miss just falls back to the deterministic keyword
    check (pure pre-scan behavior). Never raises into the hook."""
    if db is None:
        return frozenset()
    try:
        return db.security_paths(repo_root)
    except Exception:
        return frozenset()


def _history_from(db, repo_root: str, file_path: str) -> PolicyHistory:
    """This file's outcome history (per-file approvals/denials) so the scorer
    tightens or loosens on what actually happened to past edits of it.

    decide.py only READS history here — trace writes happen in the PostToolUse
    hook (hedwig-record.py) for what actually executed; the classifier write
    happens on regret. Never raises — a DB miss degrades to cold-start.
    """
    if db is None:
        return _empty_history()
    try:
        return db.policy_history(repo_root, file_path, stage="apply")
    except Exception:
        return _empty_history()


def _constraint_decision(db, repo_root: str, rel: str) -> tuple[str, str] | None:
    """Cascade layer 1 — hard constraints override everything.

    Returns (action, reason) when a stored hard constraint matches this file,
    else None (fall through to leases/scorer). Resolved for write access:
      always_deny      -> ("deny", reason)     block the edit outright
      always_check_in  -> ("check_in", reason) force the native prompt
      always_allow     -> ("allow", reason)    auto-apply, skip scoring
    Best-effort: any failure returns None so a DB hiccup never blocks an edit
    (the scorer still runs). This is the first gate, before the scorer.
    """
    if db is None:
        return None
    try:
        constraint = db.strongest_constraint(repo_root, rel, access_type="write")
    except Exception:
        return None
    if constraint is None:
        return None
    policy = constraint.policy_for("write")
    if policy == "always_deny":
        return "deny", f"hard constraint: writes to {constraint.path_pattern} are always denied"
    if policy == "always_check_in":
        return "check_in", f"hard constraint: writes to {constraint.path_pattern} always require review"
    if policy == "always_allow":
        return "allow", f"hard constraint: writes to {constraint.path_pattern} are always allowed"
    return None


def _apply_handshake(action: str, session_id, rel: str) -> tuple[str, str]:
    """Honor an agent self-declaration for this file — TIGHTEN ONLY.

    The bidirectional half of the governance handshake (R2): if the agent
    declared low confidence or explicitly requested a check-in (via
    hedwig-declare.py, prompted by the confidence-checkin skill), force this
    action to surface for developer review even when the scorer would
    auto-apply.

    Returns (action, reason). `reason` is non-empty only when the handshake
    actually changed the verdict, so the caller uses it verbatim (it's the
    agent's own stated reason — the most trust-building thing to show).

    SAFETY INVARIANT: this only ever downgrades "proceed" → "check_in". It can
    never loosen a surfaced verdict to auto-apply — an agent declaring high
    confidence does NOT earn an auto-apply the scorer didn't already grant.
    Best-effort: any lookup failure leaves the action unchanged.
    """
    if action != "proceed":
        return action, ""  # already surfacing (or flagged) — nothing to tighten
    try:
        decl = latest_self_checkin(session_id, rel)
    except Exception:
        return action, ""
    if not decl:
        return action, ""

    requesting = bool(decl.get("requesting_self_checkin"))
    confidence = decl.get("confidence")
    low_conf = isinstance(confidence, (int, float)) and confidence <= LOW_CONFIDENCE_THRESHOLD
    if not (requesting or low_conf):
        return action, ""  # agent declared, but confidently — respect the scorer

    name = rel.rsplit("/", 1)[-1]
    agent_reason = (decl.get("reason") or "").strip()
    if requesting:
        base = f"{name}: the agent asked to check in on this edit"
    else:
        base = f"{name}: the agent flagged low confidence ({float(confidence):.0%}) on this edit"
    if agent_reason:
        base += f' — "{agent_reason}"'
    return "check_in", base + " — surfacing for your review."


def _should_deny(risk, history, is_new_file: bool) -> bool:
    """R6 gate: is this surfaced edit risky enough to BLOCK with a deny+reason
    so the agent self-corrects, rather than just falling through to the native
    prompt?

    deny BLOCKS the tool call, so it's reserved for genuinely high-risk
    actions — firing it on every check-in would trade prompt-fatigue for
    retry-fatigue. Gate (any one is enough):
      * security-sensitive code,
      * high blast radius (> 3 dependents),
      * a prior reversal / verification-failure (denial) on this file.

    A brand-new file NEVER triggers a deny, regardless of why it's risky:
    first-sight files go to the human (a deny would just bounce a legitimate
    new file with "narrow this", which the agent can't act on — there's no
    smaller existing version to revise toward). deny is for *edits* Hedwig can
    ask the agent to revise, not for the creation of a new file.
    """
    if is_new_file:
        return False
    if risk.is_security_sensitive:
        return True
    if risk.blast_radius > 3:
        return True
    if history.denials > 0:
        return True
    return False


def _deny_reason(rel: str, risk, history) -> str:
    """Plain-English, actionable deny reason fed back to the agent (R6).

    Reuses the S3.5 judgment voice but phrased as a revise-or-escalate
    instruction, since with deny the agent acts on it same-turn. Leads with the
    strongest gate so the agent knows what to change."""
    name = rel.rsplit("/", 1)[-1]
    if history.denials > 0:
        why = f"a recent edit to {name} was reverted or failed a check"
    elif risk.is_security_sensitive:
        why = f"{name} is security-sensitive"
    elif risk.blast_radius > 3:
        why = f"{name} has {risk.blast_radius} dependents — a wide blast radius"
    else:
        why = f"{name} looks risky"
    return (
        f"Hedwig is holding this edit: {why}. Narrow the change to the smallest "
        f"safe step (one function / one concern), or explain why it's needed and "
        f"re-propose — otherwise it will go to the developer for review."
    )


def _read_old_content(repo_root: Path, file_path: str) -> tuple[str, bool]:
    target = repo_root / file_path
    if not target.exists():
        return "", True
    try:
        return target.read_text(), False
    except Exception:
        return "", False


def _payload_to_risk_inputs(payload: dict) -> tuple[Path, str, str, str, bool, int] | None:
    """Pull what we need to score the action from the PreToolUse payload.

    Returns (repo_root, file_path, old_content, new_content, is_new_file,
    diff_size) or None if the payload doesn't describe a governable action.
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _GOVERNED_TOOLS:
        return None

    # Defend against malformed-but-valid-JSON payloads: a non-dict tool_input,
    # a non-string file_path, or a non-list `edits` would otherwise crash the
    # hook with AttributeError/TypeError and exit non-zero, breaking the edit.
    # Anything unexpected → treat as ungovernable and pass through.
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None

    cwd = payload.get("cwd") or os.getcwd()
    repo_root = Path(cwd)

    # Resolve to repo-relative path. assess_risk and the scorer expect a
    # relative string for blast-radius scanning.
    try:
        target = Path(file_path)
        if target.is_absolute():
            rel = str(target.relative_to(repo_root))
        else:
            rel = file_path
    except ValueError:
        # File outside the repo — score it as best we can but skip blast
        # radius (estimate_blast_radius gracefully handles non-existent rels).
        rel = file_path

    if tool_name == "Write":
        new_content = tool_input.get("content") or ""
    elif tool_name == "Edit":
        # Edit replaces old_string with new_string in an existing file. We
        # approximate "new content" by reading the file and applying the
        # substitution; if the file isn't present we degrade to scoring the
        # diff alone.
        old_string = tool_input.get("old_string") or ""
        new_string = tool_input.get("new_string") or ""
        existing, _ = _read_old_content(repo_root, rel)
        new_content = existing.replace(old_string, new_string, 1) if existing else new_string
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        edits = edits if isinstance(edits, list) else []
        existing, _ = _read_old_content(repo_root, rel)
        new_content = existing
        for edit in edits:
            if not isinstance(edit, dict):
                continue  # skip malformed edit entries rather than crash
            new_content = new_content.replace(
                edit.get("old_string") or "",
                edit.get("new_string") or "",
                1,
            )
    else:
        return None

    old_content, is_new_file = _read_old_content(repo_root, rel)
    diff_size = abs(len(new_content.splitlines()) - len(old_content.splitlines()))

    return repo_root, rel, old_content, new_content, is_new_file, diff_size


def main() -> int:
    """Top-level guard: a PreToolUse hook must NEVER exit non-zero — that would
    block/error the governed edit. Any unanticipated payload shape or internal
    error falls through to a passthrough (native permission flow) rather than
    crashing. The inner function holds the real logic."""
    try:
        return _main_inner()
    except Exception:
        return _passthrough()


def _main_inner() -> int:
    # Re-exec under a deps-capable interpreter (if the current one lacks
    # numpy/sklearn) BEFORE touching stdin, so the learned classifier always
    # runs at the booth. No-op when this interpreter already has the deps, when
    # opted out, or when none is configured (then we degrade to the heuristic).
    ensure_learned_interpreter()

    raw = sys.stdin.read()
    if not raw.strip():
        return _passthrough()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _passthrough()
    if not isinstance(payload, dict):
        return _passthrough()  # valid JSON but not an object (list/str/num)

    inputs = _payload_to_risk_inputs(payload)
    if inputs is None:
        return _passthrough()

    repo_root, rel, old_content, new_content, is_new_file, diff_size = inputs

    # DB key: canonicalize through repo_root_key so constraints/preferences
    # authored by the slash commands (which key on the resolved getcwd) are
    # found here. `repo_root` stays the raw payload path for file-path math.
    repo_root_str = repo_root_key(str(repo_root))

    # Open the DB once and read history + the persisted classifier from it, so
    # the decide turn runs the full CAIS cascade: the heuristic carries
    # cold-start, the online log-reg PolicyClassifier takes over once it has
    # seen MIN_SAMPLES_FOR_LEARNED (10) real decisions (select_active_scorer).
    db = _open_db_safe()

    # Agent-flagged security paths (from /hedwig-scan) augment the deterministic
    # keyword check — additive only, never clears a keyword match, so invariant 5
    # holds. Best-effort: any failure → empty set → pure keyword behavior.
    extra_security = _security_paths_safe(db, repo_root_str)

    risk = assess_risk(
        repo_root=repo_root,
        file_path=rel,
        old_content=old_content,
        new_content=new_content,
        is_new_file=is_new_file,
        diff_size=diff_size,
        extra_security_paths=extra_security,
    )

    # Cascade layer 1: hard constraints override everything (before the scorer).
    # A developer-set always_deny/always_allow is non-negotiable; always_check_in
    # forces the native prompt. Capture the Edit substitution first so a
    # constraint deny is logged with the same shape as a scorer decision.
    _ti = payload.get("tool_input") or {}
    _c_edit_old = _ti.get("old_string") or "" if payload.get("tool_name") == "Edit" else ""
    _c_edit_new = _ti.get("new_string") or "" if payload.get("tool_name") == "Edit" else ""
    constraint = _constraint_decision(db, repo_root_str, rel)
    if constraint is not None:
        c_action, c_reason = constraint
        if c_action == "allow":
            _log_decision(
                payload, rel, "suppressed", 0.0, risk, c_reason,
                edit_old=_c_edit_old, edit_new=_c_edit_new, scorer="constraint",
            )
            _emit({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": c_reason,
                },
            })
            return 0
        if c_action == "deny":
            _log_decision(
                payload, rel, DENIED_VERDICT, 0.0, risk, c_reason,
                edit_old=_c_edit_old, edit_new=_c_edit_new, scorer="constraint",
            )
            _emit({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": c_reason,
                },
            })
            return 0
        # always_check_in: surface to the native prompt, skip the scorer entirely.
        _log_decision(
            payload, rel, "surfaced", 0.0, risk, c_reason,
            edit_old=_c_edit_old, edit_new=_c_edit_new, scorer="constraint",
        )
        return _passthrough()

    history = _history_from(db, repo_root_str, rel)
    classifier = load_classifier(db, repo_root_str) if db is not None else None

    pi = PolicyInput.from_signals(
        history,
        risk,
        recent_denials=0,
        files_in_action=1,
    )

    # Cascade layer 2: the scorer. The heuristic carries cold-start; the online
    # PolicyClassifier takes over once it has MIN_SAMPLES_FOR_LEARNED real
    # decisions (select_active_scorer).
    scorer, scorer_label = select_active_scorer(classifier)
    # Threshold scale depends on which scorer fired. The heuristic emits a raw
    # additive score (~[-3, +1]); 0.0/-1.0 keep cold-start permissive so
    # low-risk edits auto-apply immediately (the demo). The learned scorer
    # emits a calibrated approval *probability* in [0, 1]; 0.5/0.25 map that to
    # proceed / proceed_flag / check_in. Using one pair for both would
    # misthreshold whichever scorer it wasn't tuned for.
    if scorer_label == "learned":
        proceed_threshold, flag_threshold = 0.5, 0.25
    else:
        proceed_threshold, flag_threshold = 0.0, -1.0
    decision = scorer.decide(
        pi,
        proceed_threshold=proceed_threshold,
        flag_threshold=flag_threshold,
    )

    # Cascade layer 3: developer-confirmed preferences override the scorer's
    # decision (tighten by default; the one narrow auto_apply loosening exception
    # is enforced inside PreferenceCoordinator). A pattern the developer
    # confirmed via /hedwig-learn fires here. No-op when no preference matches.
    decision = apply_confirmed_preferences(db, repo_root_str, decision, risk, rel)

    # Cascade layer 4: R2 confidence handshake (tighten-only). If the agent
    # self-declared low
    # confidence or explicitly requested a check-in for this file, honor it by
    # forcing a surface — even when the scorer alone would auto-apply. This can
    # ONLY downgrade proceed → surfaced; it never loosens a surfaced verdict to
    # auto-apply (the safety invariant). Absent a declaration, behavior is
    # unchanged (today's inferred-intent path).
    action, handshake_reason = _apply_handshake(
        decision.action, payload.get("session_id"), rel
    )

    # Cascade layer 5a — deterministic security floor (invariant 5: the model
    # is untrusted). The
    # learned classifier can drift toward "approve everything" after enough
    # auto-approvals and return proceed even for a security-sensitive file —
    # auto-applying it before the _should_deny gate (which only runs on the
    # surfaced branch) ever sees it. assess_risk flags is_security_sensitive
    # deterministically; enforce it as a FLOOR here so no learned score can
    # auto-apply a security-sensitive edit. Tighten-only: downgrade proceed →
    # check_in, never the reverse. The edit then flows into the existing
    # surfaced-branch logic, where _should_deny escalates it to deny+reason.
    forced_reason = None
    if action == "proceed" and risk.is_security_sensitive:
        action = "check_in"
        forced_reason = (
            f"{rel.rsplit('/', 1)[-1]} is security-sensitive — Hedwig always "
            f"surfaces these for review, regardless of what it has learned."
        )

    reason = forced_reason or handshake_reason or _plain_reason(
        verdict=action,
        rel=rel,
        risk=risk,
        history=history,
        is_new_file=is_new_file,
    )

    # Capture the raw Edit substitution so the recorder can later recognize a
    # reversal of this auto-applied edit (R1 verification-independent regret).
    tool_input = payload.get("tool_input") or {}
    edit_old = tool_input.get("old_string") or "" if payload.get("tool_name") == "Edit" else ""
    edit_new = tool_input.get("new_string") or "" if payload.get("tool_name") == "Edit" else ""

    if action == "proceed":
        _log_decision(
            payload, rel, "suppressed", decision.score, risk, reason,
            edit_old=edit_old, edit_new=edit_new, scorer=scorer_label,
        )
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": reason,
            },
        })
        return 0

    # Cascade layer 5b — R6 deny+reason self-correction loop. For a surfaced
    # edit that trips the
    # high-risk gate, BLOCK the call with a plain-English reason so the agent
    # revises same-turn (rather than silently falling through to the native
    # prompt). Three guardrails keep this from becoming retry-fatigue:
    #   * gated: only security-sensitive / high-blast / previously-regretted
    #     edits (_should_deny) — ordinary check-ins still pass through;
    #   * capped: at most MAX_DENY_RETRIES denies per (session, file), then we
    #     defer to the human so a stubborn disagreement always escalates;
    #   * not on a handshake surface: if the AGENT asked to check in (R2), don't
    #     bounce its own request back at it — let the human see it.
    session_id = payload.get("session_id")
    handshake_forced = bool(handshake_reason)
    if (
        not handshake_forced
        and _should_deny(risk, history, is_new_file)
        and prior_deny_count(session_id, rel) < MAX_DENY_RETRIES
    ):
        deny_reason = _deny_reason(rel, risk, history)
        _log_decision(
            payload, rel, DENIED_VERDICT, decision.score, risk, deny_reason,
            edit_old=edit_old, edit_new=edit_new, scorer=scorer_label,
        )
        _emit({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": deny_reason,
            },
        })
        return 0

    # Otherwise (ordinary check-in, handshake surface, or the deny cap reached)
    # → fall through to the native prompt. The reason rides in the decisions
    # log and surfaces in /hedwig-status.
    _log_decision(
        payload, rel, "surfaced", decision.score, risk, reason,
        edit_old=edit_old, edit_new=edit_new, scorer=scorer_label,
    )
    return _passthrough()


if __name__ == "__main__":
    sys.exit(main())
