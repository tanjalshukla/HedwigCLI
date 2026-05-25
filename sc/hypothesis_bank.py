from __future__ import annotations

"""Hypothesis candidate bank — generate, accumulate evidence, surface or prune.

Design:
- Rule-based generators seed candidates into the bank as soon as their minimum
  threshold is crossed (instead of surfacing immediately as before).
- After each trace, every pending candidate is scored using context-weighted
  evidence: traces in high-similarity contexts (similar file type, blast radius,
  change pattern) contribute stronger signal than low-similarity ones.
  Inspired by CIPHER (Gao et al., NeurIPS 2024) k-nearest context retrieval.
- When confidence >= SURFACE_CONFIDENCE and total >= MIN_EVIDENCE, the candidate
  is promoted to 'ready_to_surface'.
- When confidence <= PRUNE_THRESHOLD and total >= MIN_EVIDENCE, the candidate
  is stored with status='rejected' (not deleted) so the reasoning is preserved.
- Optionally, an LLM generator runs every LLM_GENERATION_INTERVAL turns and
  can add novel candidates the rules didn't catch.

This implements the Trial-Error-Explain loop from the advisors' papers:
generate a hypothesis → observe evidence → persist or prune.
"""

import json
from .preference_inference import (
    MIN_PUSHBACK_COUNT,
    MIN_TRACES_FOR_HYPOTHESIS,
    PreferenceHypothesis,
    SessionSummary,
    _FAILURE_REACTIVE_MIN_FAILURES,
    _failure_reactive_hypothesis,
    _is_deliberate_reviewer,
    _is_rapid_approver,
    _scope_narrowing_hypothesis,
    _positive_redirect_hypothesis,
    _deliberate_reviewer_hypothesis,
    _rapid_approver_hypothesis,
)
from .preferences import (
    PushbackType,
    UserPersona,
    preference_to_dict,
)
from .trust_db import TrustDB


# Evidence thresholds.
SURFACE_CONFIDENCE = 0.70   # evidence_for / total >= this → surface
PRUNE_THRESHOLD    = 0.30   # evidence_for / total <= this → prune
# Minimum qualifying traces before a candidate can surface or be pruned.
# 3 is the minimum at which a 70% confidence claim is statistically meaningful
# (2/3 supporting traces). Was 5, which required more interactions than a
# typical demo session can produce for a single behavioral pattern.
MIN_EVIDENCE       = 3

# How often to run the LLM hypothesis generator (every N turns).
LLM_GENERATION_INTERVAL = 10


def _context_weight(driver: str, trace: dict) -> float:
    """Context similarity weight for evidence (0.5–1.5).

    Inspired by CIPHER (Gao et al., NeurIPS 2024): traces in contexts that
    are most relevant to the hypothesis provide stronger signal.
    Returns 1.0 for neutral context, >1.0 for high-signal context.
    """
    blast = trace.get("blast_radius") or 1
    change = trace.get("change_type") or ""
    response_ms = trace.get("response_time_ms") or 0

    if driver == "scope_constraint":
        # Higher blast radius = more relevant context for scope hypothesis
        if blast >= 3:
            return 1.5
        if blast == 2:
            return 1.2
        return 0.8  # single-file traces are weak signal for multi-file hypothesis

    if driver == "failure_reactive":
        # API and data model changes in a debug context = strongest signal
        if any(p in change for p in ("api_change", "data_model")):
            return 1.4
        return 1.0

    if driver == "rapid_approver":
        # Very fast responses are the core context
        if response_ms > 0 and response_ms < 2000:
            return 1.5
        if response_ms > 10000:
            return 1.5  # slow = strong contra-evidence
        return 1.0

    if driver == "deliberate_reviewer":
        if response_ms > 12000:
            return 1.4
        return 1.0

    return 1.0


def _evidence_for_trace(driver: str, trace: dict) -> tuple[int, int]:
    """Return (delta_for, delta_against) for a trace against a given driver."""
    pushback = trace.get("pushback_type") or ""
    decision = (trace.get("user_decision") or "").lower()
    blast = trace.get("blast_radius") or 1
    rubber = trace.get("rubber_stamp") == 1
    response_ms = trace.get("response_time_ms") or 0
    verif = trace.get("verification_passed")

    if driver == "scope_constraint":
        if pushback == PushbackType.SCOPE_CONSTRAINT.value:
            return 1, 0
        if decision.startswith("auto_approve") and blast > 1:
            return 0, 1
        return 0, 0

    if driver == "failure_reactive":
        if pushback == PushbackType.FAILURE_REPORT.value:
            return 1, 0
        if verif == 0:
            return 1, 0
        if verif == 1:
            return 0, 1
        return 0, 0

    if driver == "rapid_approver":
        if rubber and response_ms < 3000:
            return 1, 0
        if response_ms > 8000:
            return 0, 1
        return 0, 0

    if driver == "deliberate_reviewer":
        if response_ms > 8000 and not rubber:
            return 1, 0
        if rubber:
            return 0, 1
        return 0, 0

    if driver == "positive_redirect":
        if pushback == PushbackType.POSITIVE_REDIRECT.value:
            return 1, 0
        if pushback in (PushbackType.CORRECTION.value, PushbackType.REJECTION.value):
            return 0, 1
        return 0, 0

    return 0, 0


def _candidate_confidence(evidence_for: int, evidence_against: int) -> float:
    total = evidence_for + evidence_against
    if total == 0:
        return 0.5
    return evidence_for / total


def seed_candidates_from_session(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
    session_summary: SessionSummary,
    pushback_counts: dict[str, int],
    recent_verification_failures: int = 0,
    inferred_persona: UserPersona | None = None,
) -> list[int]:
    """Generate rule-based hypothesis candidates and add them to the bank.

    Unlike the old system, this does NOT surface them — it just seeds them.
    Returns the IDs of newly added candidates.
    """
    if session_summary.n_turns < MIN_TRACES_FOR_HYPOTHESIS:
        return []
    # Note: DELEGATING sessions still seed candidates so evidence can accumulate.
    # The surfacing gate (get_ready_hypothesis called from apply_stage) checks
    # intensity before showing the panel — delegating sessions won't be interrupted.
    # This matches the doc claim: "gate prevents interruption, not learning."

    new_ids: list[int] = []

    def _seed(hyp: PreferenceHypothesis) -> None:
        if trust_db.candidate_driver_exists(repo_root, session_id, hyp.driver):
            return
        cid = trust_db.add_hypothesis_candidate(
            repo_root=repo_root,
            session_id=session_id,
            driver=hyp.driver,
            source="rule_based",
            prompt=hyp.prompt,
            rationale=hyp.rationale,
            preference_json=json.dumps(preference_to_dict(hyp.proposed_preference)),
        )
        new_ids.append(cid)

    scope_count = pushback_counts.get(PushbackType.SCOPE_CONSTRAINT.value, 0)
    if scope_count >= MIN_PUSHBACK_COUNT:
        _seed(_scope_narrowing_hypothesis(scope_count))

    total_failures = session_summary.n_failures + max(0, recent_verification_failures)
    if total_failures >= _FAILURE_REACTIVE_MIN_FAILURES:
        _seed(_failure_reactive_hypothesis(total_failures))

    if _is_deliberate_reviewer(session_summary):
        _seed(_deliberate_reviewer_hypothesis(session_summary.n_approvals))

    if _is_rapid_approver(session_summary):
        _seed(_rapid_approver_hypothesis(session_summary.n_approvals))

    positive_count = pushback_counts.get(PushbackType.POSITIVE_REDIRECT.value, 0)
    if positive_count >= MIN_PUSHBACK_COUNT:
        _seed(_positive_redirect_hypothesis(positive_count))

    return new_ids


def update_evidence(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
    trace: dict,
) -> list[int]:
    """Score latest trace against all pending candidates with context weighting.

    Context weight (CIPHER-inspired): traces in highly-relevant contexts
    contribute stronger signal. A scope_constraint trace on a 5-file change
    counts more than one on a single-file change. Weights are applied by
    accumulating fractional evidence rounded to nearest integer per trace.

    Returns IDs of candidates newly promoted to 'ready_to_surface'.
    """
    candidates = trust_db.get_pending_hypothesis_candidates(repo_root, session_id)
    ready: list[int] = []

    for row in candidates:
        cid = int(row["id"])
        driver = row["driver"]
        raw_for, raw_against = _evidence_for_trace(driver, trace)

        # Apply context weight — high-relevance traces count for more.
        if raw_for or raw_against:
            weight = _context_weight(driver, trace)
            # Round weighted evidence to nearest integer (min 1 if original signal was 1)
            delta_for = round(raw_for * weight) if raw_for else 0
            delta_against = round(raw_against * weight) if raw_against else 0
            # Ensure at least 1 if raw signal was present
            if raw_for and delta_for == 0:
                delta_for = 1
            if raw_against and delta_against == 0:
                delta_against = 1
            trust_db.update_hypothesis_evidence(
                cid, delta_for=delta_for, delta_against=delta_against
            )
        else:
            delta_for, delta_against = 0, 0

        # Use pre-update snapshot + delta rather than re-reading from DB.
        # This avoids a second SELECT round-trip and is correct for the
        # single-threaded main path. The daemon LLM thread only INSERTs
        # new candidates; it never updates evidence_for/against, so there
        # is no TOCTOU race on these fields.
        new_for = int(row["evidence_for"]) + delta_for
        new_against = int(row["evidence_against"]) + delta_against
        total = new_for + new_against

        if total < MIN_EVIDENCE:
            continue

        confidence = new_for / total
        if confidence >= SURFACE_CONFIDENCE:
            trust_db.set_hypothesis_status(cid, "ready_to_surface")
            ready.append(cid)
        elif confidence <= PRUNE_THRESHOLD:
            # Store as 'rejected' not deleted — TICL-inspired negative example
            # preservation. The contradicting evidence stays in the bank so future
            # LLM generation and /prefs display can show "I considered this but
            # evidence didn't support it." Nothing is silently discarded.
            trust_db.set_hypothesis_status(cid, "rejected")

    return ready


def get_ready_hypothesis(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
) -> "PreferenceHypothesis | None":
    """Return the highest-priority candidate that's ready to surface, or None."""
    from .preferences import preference_from_dict
    from .preference_inference import PreferenceHypothesis

    with trust_db._connect() as conn:
        rows = conn.execute(
            """
            SELECT id, driver, prompt, rationale, preference_json,
                   evidence_for, evidence_against
            FROM hypothesis_candidates
            WHERE repo_root = ? AND session_id = ? AND status = 'ready_to_surface'
            ORDER BY evidence_for DESC
            LIMIT 1
            """,
            (repo_root, session_id),
        ).fetchall()

    if not rows:
        return None

    row = rows[0]
    try:
        pref = preference_from_dict(json.loads(row["preference_json"]))
    except Exception:
        return None

    confidence = _candidate_confidence(
        int(row["evidence_for"]), int(row["evidence_against"])
    )
    return PreferenceHypothesis(
        prompt=row["prompt"],
        rationale=(
            f"{row['rationale']}  "
            f"[{int(row['evidence_for'])}/{int(row['evidence_for']) + int(row['evidence_against'])} "
            f"turns support this — {confidence:.0%} confidence]"
        ),
        proposed_preference=pref,
        driver=row["driver"],
    )


def mark_candidate_surfaced(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
    driver: str,
    confirmed: bool,
) -> None:
    """Mark candidate as surfaced + confirmed/declined after developer responds."""
    status = "confirmed" if confirmed else "declined"
    with trust_db._connect() as conn:
        conn.execute(
            """
            UPDATE hypothesis_candidates
            SET status = ?
            WHERE repo_root = ? AND session_id = ? AND driver = ?
              AND status = 'ready_to_surface'
            """,
            (status, repo_root, session_id, driver),
        )


def maybe_generate_llm_hypotheses(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
    session_summary: SessionSummary,
    turn_count: int,
    client,
) -> list[int]:
    """Every LLM_GENERATION_INTERVAL turns, ask Claude if it sees novel patterns.

    Returns IDs of newly added candidates. No-ops if client is None or if
    the turn count isn't at an interval boundary.
    """
    if client is None:
        return []
    if turn_count == 0 or turn_count % LLM_GENERATION_INTERVAL != 0:
        return []

    prompt = (
        f"You are analyzing a developer's interaction patterns with a coding agent.\n\n"
        f"Session summary:\n"
        f"- Turns: {session_summary.n_turns}\n"
        f"- Approvals: {session_summary.n_approvals}, Denials: {session_summary.n_denials}\n"
        f"- Failures reported: {session_summary.n_failures}\n"
        f"- Feedback turns: {session_summary.n_feedback}\n"
        f"- Mean review time: {session_summary.mean_review_seconds:.1f}s\n"
        f"- Approval rate: {session_summary.approval_rate:.0%}\n\n"
        "Generate up to 2 novel behavioral hypotheses about this developer's preferences "
        "that are NOT already covered by these known patterns: "
        "scope_constraint, failure_reactive, deliberate_reviewer, rapid_approver, positive_redirect.\n\n"
        "For each hypothesis, return JSON:\n"
        "{\"driver\": \"unique_name\", \"prompt\": \"question to ask developer\", "
        "\"rationale\": \"one sentence why\"}\n\n"
        "Return a JSON array. If no novel hypothesis is warranted, return []."
    )

    try:
        import re
        from .session import ClaudeSession
        _session = ClaudeSession(system_prompt="You are a behavioral pattern analyst. Return JSON only.")
        _session.add_user(prompt)
        raw = client._call(_session, max_tokens=400, temperature=0.3)
        # Non-greedy: match the first complete [...] block.
        # Greedy would span from first '[' to last ']', swallowing any
        # markdown list in the LLM preamble before the actual JSON array.
        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if not match:
            return []
        candidates_data = json.loads(match.group())
    except Exception:
        return []

    new_ids: list[int] = []
    for item in candidates_data[:2]:
        driver = item.get("driver", "").strip()
        hyp_prompt = item.get("prompt", "").strip()
        rationale = item.get("rationale", "").strip()
        if not driver or not hyp_prompt:
            continue
        if trust_db.candidate_driver_exists(repo_root, session_id, driver):
            continue

        # LLM-generated hypotheses use a generic SOFT_CHECKIN preference.
        from .preferences import (
            Condition, Lifecycle, Preference, PreferenceAction, Scope, Trigger
        )
        pref = Preference(
            trigger=Trigger(stages=("apply",)),
            condition=Condition(),
            action=PreferenceAction.SOFT_CHECKIN,
            scope=Scope(level="repo"),
            lifecycle=Lifecycle(provenance="inferred_user_confirmed"),
        )
        cid = trust_db.add_hypothesis_candidate(
            repo_root=repo_root,
            session_id=session_id,
            driver=driver,
            source="llm_generated",
            prompt=hyp_prompt,
            rationale=rationale,
            preference_json=json.dumps(preference_to_dict(pref)),
        )
        new_ids.append(cid)

    return new_ids
