"""Shared helpers for Hedwig plugin hook scripts.

Kept dependency-free and tiny. Every hook script (decide, record, sentinel,
status) resolves the plugin data dir the same way and several append JSONL
event rows — factor that here so the behavior can't drift between scripts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _ensure_vendor_on_path() -> None:
    """Put plugin/vendor on sys.path so `import sc` resolves to the bundled
    copy. Idempotent. bin/ -> plugin/ -> plugin/vendor."""
    vendor = Path(__file__).resolve().parent.parent / "vendor"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))


# --- Learned-scorer interpreter shim -----------------------------------------
#
# Claude Code launches these hooks via their `#!/usr/bin/env python3` shebang,
# so they run under whatever `python3` is first on PATH — usually a bare system
# interpreter with no numpy / scikit-learn. There the online PolicyClassifier
# can't load and Hedwig silently runs heuristic-only. To make the learned
# scorer ALWAYS run when a capable interpreter exists, the classifier-touching
# hooks (decide / record / verify) re-exec themselves under one *before*
# reading stdin. `python3 bin/hedwig-setup.py` builds that interpreter at the
# fixed path below; $HEDWIG_PYTHON overrides it; $HEDWIG_NO_REEXEC opts out.
_REEXEC_SENTINEL = "HEDWIG_REEXEC"   # set on the child so a re-exec never loops
_NO_REEXEC = "HEDWIG_NO_REEXEC"      # opt-out (tests pin to the current interp)
_BOOTH_VENV = Path.home() / ".hedwig" / "venv" / "bin" / "python"


def _interpreter_has_learned_deps() -> bool:
    """True if THIS interpreter can import the classifier's deps (numpy +
    scikit-learn). fastembed is retrieval-only, not required for the scorer."""
    try:
        import numpy  # noqa: F401,PLC0415
        import sklearn  # noqa: F401,PLC0415

        return True
    except Exception:
        return False


def _python_can_import_deps(python_path: str) -> bool:
    """Probe: does `python_path` actually import numpy + sklearn?

    os.execv only fails if the target file is missing/non-executable — a venv
    that exists but is broken (half-built by an interrupted setup, base
    interpreter relocated, wrong arch after a machine migration) execs FINE and
    then dies in the replacement process, which the parent can't trap: the
    stdin payload is gone and the hook exits non-zero, breaking the user's edit.
    So we must confirm the deps load BEFORE handing the process over, not just
    that the file exists. Short timeout; any failure → not capable."""
    try:
        result = subprocess.run(
            [python_path, "-c", "import numpy, sklearn"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _resolve_capable_python() -> str | None:
    """A python interpreter VERIFIED to have the learned-scorer deps, or None.

    Order: $HEDWIG_PYTHON (explicit override), then the fixed setup venv
    (~/.hedwig/venv) that hedwig-setup.py creates. Each candidate is probed
    (_python_can_import_deps) — existence alone is not enough, because execv
    into a broken-but-present interpreter loses the payload and can't be
    unwound. Never returns the current interpreter — that's the one we're
    trying to escape."""
    candidates = []
    env_python = os.environ.get("HEDWIG_PYTHON")
    if env_python:
        candidates.append(env_python)
    candidates.append(str(_BOOTH_VENV))
    for cand in candidates:
        try:
            if (
                cand
                and cand != sys.executable
                and Path(cand).exists()
                and _python_can_import_deps(cand)
            ):
                return cand
        except Exception:
            continue
    return None


def learned_scorer_reachable() -> bool:
    """True if the learned classifier can run on a governed edit — i.e. either
    this interpreter already has the deps, or a capable re-exec target exists
    ($HEDWIG_PYTHON or the ~/.hedwig/venv that hedwig-setup.py builds).

    False means every hook will degrade to the heuristic forever until the user
    runs setup. Used by /hedwig-status to nudge — never affects a decision.
    Best-effort: any failure reports False (the conservative 'suggest setup')."""
    try:
        if _interpreter_has_learned_deps():
            return True
        return _resolve_capable_python() is not None
    except Exception:
        return False


def ensure_learned_interpreter() -> None:
    """Re-exec this hook under a deps-capable interpreter so the learned
    classifier always runs, when the current interpreter can't load it.

    MUST be called before any stdin read — os.execv inherits fd 0, so the
    replacement process reads the hook payload fresh. Fires at most once
    (guarded by a sentinel env var, so a misconfigured target can't loop).

    No-op — stays on the current interpreter, degrading cleanly to the
    heuristic — when: opted out ($HEDWIG_NO_REEXEC), already re-exec'd once,
    the current interpreter already has the deps, or no capable interpreter is
    found. Any failure falls through to the current interpreter; never raises.
    """
    if os.environ.get(_NO_REEXEC) or os.environ.get(_REEXEC_SENTINEL):
        return
    if _interpreter_has_learned_deps():
        return  # classifier will load right here — nothing to do
    target = _resolve_capable_python()
    if not target:
        return  # no capable interpreter configured — degrade to heuristic
    try:
        os.environ[_REEXEC_SENTINEL] = "1"
        os.execv(target, [target, *sys.argv])
    except Exception:
        # Re-exec failed (permissions, bad path, …) — run here and degrade.
        os.environ.pop(_REEXEC_SENTINEL, None)


def open_trust_db():
    """Open the per-install Hedwig SQLite DB at <data_dir>/trust.db.

    DEPENDENCY CONTRACT (REVISED 2026-06-25, S5): the default plugin path is
    SQLite-backed and learns locally — constructing TrustDB, recording traces,
    AND loading/updating/saving the PolicyClassifier (online logistic
    regression) all run here. numpy + scikit-learn + fastembed are sanctioned
    deps on this path (they ship with the plugin). What must NEVER be pulled on
    the decide path: torch, anthropic, boto. Regret is now routed BOTH as a
    negative-outcome trace AND as classifier.update(approved=False) — the CAIS
    mechanism — see update_classifier_for_regret().
    """
    _ensure_vendor_on_path()
    from sc.trust_db import TrustDB  # noqa: PLC0415 — lazy so non-DB hooks stay light

    return TrustDB(data_dir() / "trust.db")


def load_classifier(db, repo_root: str):
    """Load the per-repo PolicyClassifier, building+persisting a cold one if
    none exists yet. Mirrors apply_stage.py's load-or-seed step.

    Best-effort: returns None on any failure so the caller can fall back to the
    pure-stdlib heuristic path (a classifier hiccup must never block an edit).
    """
    _ensure_vendor_on_path()
    try:
        from sc.ml_policy import build_cold_classifier  # noqa: PLC0415

        loaded = db.load_policy_model(repo_root)
        if loaded is not None:
            return loaded
        classifier = build_cold_classifier()
        db.save_policy_model(repo_root, classifier)
        return classifier
    except Exception:
        return None


def select_active_scorer(classifier):
    """Return (scorer, label) via the CAIS select_scorer cascade: the heuristic
    carries cold-start, the learned classifier takes over once it reports
    ready() (>= MIN_SAMPLES_FOR_LEARNED real decisions). Falls back to the bare
    heuristic if anything goes wrong."""
    _ensure_vendor_on_path()
    try:
        from sc.policy import select_scorer  # noqa: PLC0415

        return select_scorer(classifier)
    except Exception:
        from sc.policy import HeuristicScorer  # noqa: PLC0415

        return HeuristicScorer(), "heuristic"


def policy_input_for_regret(db, repo_root: str, session_id: str, file_path: str):
    """Reconstruct a PolicyInput for a regret on (repo, file), or None.

    Mirrors apply_stage._apply_regret_corrections: the negative gradient is
    applied to the file's feature profile, using its per-file outcome history
    plus the most recent recorded trace's diff_size / blast_radius /
    change_type. effective_approvals is decremented by one so the update
    counteracts the original auto-approve rather than scoring on top of it.
    Best-effort: returns None on any failure (caller then skips the classifier
    update but still records the negative-outcome trace).
    """
    _ensure_vendor_on_path()
    try:
        from sc.features import parse_change_type_label  # noqa: PLC0415
        from sc.policy import PolicyInput  # noqa: PLC0415

        history = db.policy_history(repo_root, file_path, stage="apply")
        # Most recent trace for this file in the session, for the risk fields.
        regret_row = None
        for row in reversed(db.session_traces(repo_root, session_id)):
            if row["file_path"] == file_path:
                regret_row = row
                break
        diff_size = int((regret_row["diff_size"] if regret_row else 0) or 0)
        blast_radius = int((regret_row["blast_radius"] if regret_row else 1) or 1)
        regret_is_new_file, change_pattern = parse_change_type_label(
            (regret_row["change_type"] if regret_row else None)
        )
        return PolicyInput(
            prior_approvals=max(0.0, history.effective_approvals - 1),
            prior_denials=history.denials,
            avg_response_ms=history.avg_response_ms,
            avg_edit_distance=history.avg_edit_distance or 0.0,
            diff_size=diff_size,
            blast_radius=blast_radius,
            is_new_file=regret_is_new_file,
            is_security_sensitive=False,
            change_pattern=change_pattern,
            recent_denials=0,
            files_in_action=1,
        )
    except Exception:
        return None


def update_classifier_for_regret(db, repo_root: str, pi, regret_key) -> None:
    """Replay a regret as a negative classifier update, exactly once.

    The CAIS corrective gradient (apply_stage._apply_regret_corrections):
    classifier.update(pi, approved=False, count_sample=False) — count_sample
    False because a regret is a correction, not a fresh developer decision (it
    must not push the sample count past MIN_SAMPLES_FOR_LEARNED). regret_key
    (a stable trace identifier) is recorded in the classifier's persisted
    _corrected_regret_ids so the same regret never fires twice across the
    repo's lifetime. Best-effort: never raises into the hook.
    """
    if pi is None:
        return
    try:
        classifier = load_classifier(db, repo_root)
        if classifier is None:
            return
        if regret_key is not None and regret_key in classifier._corrected_regret_ids:
            return  # already corrected once — do not re-apply the gradient
        classifier.update(pi, approved=False, count_sample=False)
        if regret_key is not None:
            classifier._corrected_regret_ids.add(regret_key)
        db.save_policy_model(repo_root, classifier)
    except Exception:
        pass


def policy_input_for_decision(db, repo_root: str, file_path: str, decision_row: dict):
    """Reconstruct the PolicyInput a logged decision scored on, or None.

    decide.py logs the full RiskSignals (change_pattern + blast_radius +
    is_new_file + is_security_sensitive + diff_size) with each decision; pair
    them with the file's current per-file history to rebuild the exact scorer
    input. Used by update_classifier_for_decision to replay an executed
    auto-apply / approve as a positive learning sample. Best-effort: None on any
    failure (caller then skips the classifier update but keeps the trace).
    """
    _ensure_vendor_on_path()
    try:
        from sc.features import RiskSignals  # noqa: PLC0415
        from sc.policy import PolicyInput  # noqa: PLC0415

        history = db.policy_history(repo_root, file_path, stage="apply")
        risk = RiskSignals(
            change_pattern=str(decision_row.get("change_pattern") or "general_change"),
            blast_radius=int(decision_row.get("blast_radius") or 1),
            is_security_sensitive=bool(decision_row.get("is_security_sensitive")),
            is_new_file=bool(decision_row.get("is_new_file")),
            diff_size=int(decision_row.get("diff_size") or 0),
        )
        return PolicyInput.from_signals(
            history, risk, recent_denials=0, files_in_action=1
        )
    except Exception:
        return None


def update_classifier_for_decision(db, repo_root: str, pi, *, approved: bool) -> None:
    """Replay an executed developer decision as one positive/negative sample.

    This is the plugin analogue of apply_stage._update_classifier and the ONLY
    place sample_count grows on the plugin path — without it the online
    classifier never reaches ready() and the learned scorer can never take over
    (select_scorer stays on the heuristic forever). Called from the PostToolUse
    recorder when a governed edit actually executed: a suppressed (auto-applied)
    or surfaced-then-approved edit is positive history. count_sample defaults to
    True so each executed decision advances toward MIN_SAMPLES_FOR_LEARNED.
    Best-effort: never raises into the hook.
    """
    if pi is None:
        return
    try:
        classifier = load_classifier(db, repo_root)
        if classifier is None:
            return
        classifier.update(pi, approved=approved)
        db.save_policy_model(repo_root, classifier)
    except Exception:
        pass


def data_dir() -> Path:
    """Resolve the plugin's persistent data dir.

    Claude Code sets ${CLAUDE_PLUGIN_DATA} for installed plugins (it survives
    plugin updates). Fall back to a stable default so the scripts are also
    runnable in tests and ad-hoc invocations.
    """
    raw = os.environ.get("CLAUDE_PLUGIN_DATA")
    if raw:
        return Path(raw)
    return Path.home() / ".claude" / "plugins" / "data" / "hedwig"


def append_jsonl(filename: str, record: dict) -> None:
    """Append one JSON record to <data_dir>/<filename>. Best-effort: never
    raise, since a logging failure must not break a hook."""
    record.setdefault("ts", time.time())
    try:
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        with (d / filename).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


# The decision-event log decide.py writes and status.py reads. One row per
# governed PreToolUse, recording whether Hedwig suppressed the prompt
# (auto-applied) or surfaced it for review.
DECISIONS_LOG = "decisions.jsonl"

# The confidence-handshake log hedwig-declare.py writes and decide.py reads.
# One row per agent self-declaration (confidence / self-checkin request) for a
# (session, file). decide.py honors the most recent matching row — tighten-only.
SELF_CHECKINS_LOG = "self_checkins.jsonl"

# A self-declared confidence at or below this is treated as the agent asking
# for review. Conservative: only genuinely low confidence forces a surface, so
# a routine 0.9 declaration changes nothing.
LOW_CONFIDENCE_THRESHOLD = 0.5


def _iter_jsonl(filename: str, *, reverse: bool = False):
    """Yield parsed JSON dicts from <data_dir>/<filename>, skipping blanks and
    bad lines. Best-effort: stops iteration on any I/O error rather than
    raising, so callers never crash on a missing or corrupt log file."""
    path = data_dir() / filename
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    seq = reversed(lines) if reverse else lines
    for line in seq:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(row, dict):
            yield row


def latest_self_checkin(session_id, file_path):
    """Most recent self-declaration for this (session_id, file_path), or None.

    Reads self_checkins.jsonl bottom-up. Pure stdlib, best-effort: any read or
    parse failure returns None (→ decide.py proceeds as if no declaration was
    made, i.e. today's inferred-intent behavior). Never raises.
    """
    for row in _iter_jsonl(SELF_CHECKINS_LOG, reverse=True):
        if row.get("session_id") == session_id and row.get("file_path") == file_path:
            return row
    return None


# R6 — deny+reason self-correction loop.
#
# A surfaced (check_in) verdict for a GATED high-risk edit is escalated to a
# blocking permissionDecision:"deny" with a plain-English reason, so the agent
# revises same-turn (GATE Q1: deny+reason feeds back to Claude). Because deny
# BLOCKS the call, it is reserved for genuinely high-risk actions — firing it on
# every check-in would trade prompt-fatigue for retry-fatigue. After a capped
# number of denies on the same (session, file) we stop denying and fall through
# to the native prompt, so a stubborn disagreement always escalates to the human
# rather than looping forever.
MAX_DENY_RETRIES = 2  # deny at most twice per (session, file); then defer to human

# The verdict string decide.py logs to DECISIONS_LOG when it emitted a deny.
# Distinct from "suppressed"/"surfaced" so /hedwig-status and the retry counter
# can tell a self-correction deny apart from a passthrough surface.
DENIED_VERDICT = "denied"


def prior_deny_count(session_id, file_path) -> int:
    """How many times decide.py has already denied this (session, file).

    Counts DENIED_VERDICT rows in decisions.jsonl so the retry cap survives
    across the stateless per-call hook invocations. Best-effort: any failure
    returns 0 (the caller then treats it as 'not yet at the cap', which only
    risks one extra deny — never a missed escalation that matters for safety,
    since deny is the cautious direction)."""
    return sum(
        1
        for row in _iter_jsonl(DECISIONS_LOG)
        if (
            row.get("session_id") == session_id
            and row.get("file_path") == file_path
            and row.get("verdict") == DENIED_VERDICT
        )
    )
