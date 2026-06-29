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
from .store.types import DecisionTraceRow
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
# Booth-tuned: 5 means a typical visitor hits one noticer fire during a
# 5-minute demo. Production-tuned would be ~10 to limit Bedrock spend.
LLM_GENERATION_INTERVAL = 5


def _extract_json_array(raw: str) -> str | None:
    """Extract the outermost JSON array from a possibly-noisy LLM response.

    String-aware bracket balancing — a non-greedy regex locks onto inner
    arrays like `evidence_trace_ids`, and a naive bracket counter
    mis-counts when the LLM puts brackets inside string values like
    "Should I [pause] X?". Returns the array substring (including the
    outer brackets) or None if no balanced array is found.
    """
    start = raw.find("[")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return None


def _context_weight(driver: str, trace: DecisionTraceRow) -> float:
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
            return 0.5  # slow = strong contra-evidence (low weight)
        return 1.0

    if driver == "deliberate_reviewer":
        if response_ms > 12000:
            return 1.4
        return 1.0

    return 1.0


def _evidence_for_trace(driver: str, trace: DecisionTraceRow) -> tuple[int, int]:
    """Return (delta_for, delta_against) for a trace against a given driver."""
    pushback = trace.get("pushback_type") or ""
    decision = (trace.get("user_decision") or "").lower()
    blast = trace.get("blast_radius") or 1
    rubber = trace.get("rubber_stamp") == 1
    response_ms = trace.get("response_time_ms") or 0
    verif = trace.get("verification_passed")

    if driver in ("scope_constraint", "scope_narrow_when_tests_bundled"):
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

        # Per-candidate floor (set by the LLM noticer for high-stakes
        # hypotheses) raises the surfacing bar above MIN_EVIDENCE; it
        # cannot lower it. NULL → use global.
        candidate_floor = row["min_evidence"] if "min_evidence" in row.keys() else None
        floor = max(MIN_EVIDENCE, int(candidate_floor)) if candidate_floor else MIN_EVIDENCE

        if total < floor:
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
            WHERE repo_root = ? AND status = 'ready_to_surface'
            ORDER BY evidence_for DESC
            LIMIT 1
            """,
            (repo_root,),
        ).fetchall()

    if not rows:
        return None

    row = rows[0]
    try:
        pref_data = json.loads(row["preference_json"]) if row["preference_json"] else {}
    except Exception:
        return None

    candidate_type = pref_data.get("type", "preference")

    if candidate_type == "behavioral_guideline":
        # Behavioral guidelines don't have a Preference object; use a stub so the
        # PreferenceHypothesis dataclass is satisfied. apply_stage routing reads
        # preference_json directly and never dereferences proposed_preference for
        # this type.
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
    else:
        try:
            pref = preference_from_dict(pref_data)
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
        evidence_for=int(row["evidence_for"]),
        evidence_against=int(row["evidence_against"]),
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
            WHERE repo_root = ? AND driver = ?
              AND status = 'ready_to_surface'
            """,
            (status, repo_root, driver),
        )


_TRACE_DIGEST_LIMIT = 30


def _format_trace_digest(traces: list) -> str:
    """One line per trace: [id] file · stage · decision · pushback · note.

    Truncated to the most recent _TRACE_DIGEST_LIMIT rows. Older rows are
    summarized as a count, since the noticer cares about recent behavior.
    """
    if not traces:
        return "(no traces yet)"
    head = traces[:-_TRACE_DIGEST_LIMIT] if len(traces) > _TRACE_DIGEST_LIMIT else []
    tail = traces[-_TRACE_DIGEST_LIMIT:]
    lines: list[str] = []
    if head:
        lines.append(f"... {len(head)} earlier traces omitted ...")
    for t in tail:
        tid = t["id"]
        path = (t["file_path"] or "")[-40:]
        stage = t["stage"] or ""
        decision = (t["user_decision"] or "")[:14]
        pushback = (t["pushback_type"] or "")[:18]
        note = (t["user_feedback_text"] or "").strip().replace("\n", " ")[:60]
        bits = [f"[{tid}]", path, stage, decision]
        if pushback:
            bits.append(pushback)
        if note:
            bits.append(f'"{note}"')
        lines.append(" · ".join(bits))
    return "\n".join(lines)


def _format_active_candidates(candidates: list) -> str:
    if not candidates:
        return "(none)"
    return "\n".join(f"- {row['driver']}: {row['prompt']}" for row in candidates)


def maybe_generate_llm_hypotheses(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
    session_summary: SessionSummary,
    turn_count: int,
    client,
) -> list[int]:
    """Every LLM_GENERATION_INTERVAL turns, ask Claude to read the trace
    digest and propose novel hypotheses with cited evidence.

    Each candidate must cite ≥1 real trace ID; uncited or hallucinated cites
    are dropped. Candidates citing ≥ MIN_EVIDENCE valid traces seed straight
    into the bank with that evidence count, so high-confidence proposals
    surface immediately. Lower-evidence proposals sit alongside rule-based
    candidates and accumulate normally.
    """
    if client is None:
        return []
    if turn_count == 0 or turn_count % LLM_GENERATION_INTERVAL != 0:
        return []

    # Normalize sqlite3.Row -> dict: ingest_llm_hypotheses' logic_note branch
    # uses t.get("file_path"), which Rows don't support. The plugin path already
    # passes dicts; do the same here so both callers feed dicts.
    traces = [dict(t) for t in trust_db.session_traces(repo_root, session_id)]
    if len(traces) < MIN_EVIDENCE:
        return []
    valid_trace_ids = {int(t["id"]) for t in traces}
    digest = _format_trace_digest(traces)
    active = trust_db.get_pending_hypothesis_candidates(repo_root, session_id)
    active_block = _format_active_candidates(active)

    prompt = (
        "You are reviewing how a developer interacts with a coding agent. "
        "Study the traces and propose up to 3 observations in any of these categories:\n"
        "1. logic_note — a fact about the codebase visible from how files are used "
        "   (e.g. 'tests live in demo_recipe_api/tests/', 'models.py and store.py always change together')\n"
        "2. behavioral_guideline — a coding style pattern the developer consistently enforces "
        "   (e.g. 'developer prefers small focused functions', 'developer avoids bundling test changes with service changes')\n"
        "3. preference — a governance rule about when to pause "
        "   (e.g. 'pause before writes to auth.py', 'soft-check-in on test file writes')\n\n"
        f"Recent traces (each line is one decision):\n{digest}\n\n"
        f"Already-pending candidates (do NOT duplicate):\n{active_block}\n\n"
        "Rules:\n"
        "- Each item must cite specific trace IDs as evidence (the [id] "
        "  prefix in each line). Without citations, your observation is junk.\n"
        "- Stay grounded in the developer's behavior or in properties of "
        "  the code visible from these traces.\n"
        "- Skip drivers already covered by rule-based generators: "
        "  scope_constraint, failure_reactive, deliberate_reviewer, "
        "  rapid_approver, positive_redirect.\n\n"
        "Output JSON array, each item:\n"
        '{"type": "logic_note"|"behavioral_guideline"|"preference", '
        '"text": "the content or yes/no question (for preference)", '
        '"driver": "snake_case_unique_name", '
        '"rationale": "one sentence grounded in the cited traces", '
        '"evidence_trace_ids": [12, 17, 19]}\n\n'
        "For preference type only, also include:\n"
        '  "high_stakes": true  — set ONLY when wrongly applying would touch '
        "security-sensitive paths (auth, secrets, credentials) or proposes "
        "auto-approving without review. Otherwise omit it. "
        "High-stakes preferences require more evidence before surfacing.\n\n"
        "If nothing rises above noise, return []."
    )

    try:
        from .session import ClaudeSession
        _session = ClaudeSession(
            system_prompt="You are a behavioral-pattern analyst. Return JSON only."
        )
        _session.add_user(prompt)
        raw = client._call(_session, max_tokens=800, temperature=0.3)
        extracted = _extract_json_array(raw)
        if extracted is None:
            return []
        candidates_data = json.loads(extracted)
    except Exception:
        return []

    return ingest_llm_hypotheses(
        trust_db=trust_db,
        repo_root=repo_root,
        session_id=session_id,
        candidates_data=candidates_data,
        traces=traces,
        valid_trace_ids=valid_trace_ids,
    )


def ingest_llm_hypotheses(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
    candidates_data: list,
    traces: list,
    valid_trace_ids: set[int],
) -> list[int]:
    """Ingest LLM-proposed hypothesis candidates into the bank, with citation
    validation. Shared by two callers: the CLI's Bedrock noticer
    (maybe_generate_llm_hypotheses) and the plugin's agent-skill noticer
    (hedwig-notice.py) — the only difference between them is HOW the JSON
    candidates were produced (a Bedrock call vs. Claude Code reasoning in the
    skill). The grounding rule is identical: each candidate must cite ≥1 real
    trace ID, or it is dropped (the anti-hallucination gate). ``candidates_data``
    is the parsed JSON array; ``valid_trace_ids`` is the set of real trace IDs
    citations are checked against. Returns the new candidate IDs created."""
    from .preferences import (
        Condition, Lifecycle, Preference, PreferenceAction, Scope, Trigger
    )

    if not isinstance(candidates_data, list):
        return []

    new_ids: list[int] = []
    for item in candidates_data[:3]:
        if not isinstance(item, dict):
            continue
        driver = (item.get("driver") or "").strip()
        rationale = (item.get("rationale") or "").strip()
        cited = item.get("evidence_trace_ids") or []
        if not isinstance(cited, list):
            cited = []
        if not driver:
            continue
        # Validate citations against the trace store. An observation with no
        # real traces backing it is dropped — that's the "no hallucinated
        # evidence" gate. We keep candidates that cite ≥1 valid trace; the
        # remaining citations get pruned silently.
        # Accept int or float (JSON allows both for whole numbers).
        valid_cites: list[int] = []
        for tid in cited:
            if isinstance(tid, bool):  # bool is a subclass of int — exclude
                continue
            if isinstance(tid, (int, float)):
                t = int(tid)
                if t in valid_trace_ids:
                    valid_cites.append(t)
        if not valid_cites:
            continue

        item_type = item.get("type", "preference")

        if item_type == "logic_note":
            # Auto-store directly — no confirmation needed.
            text = (item.get("text") or "").strip()
            if text and valid_cites:
                file_paths = [
                    str(t.get("file_path", ""))
                    for t in traces
                    if int(t.get("id", 0)) in set(valid_cites)
                ]
                trust_db.add_logic_notes(
                    repo_root,
                    source="llm_inferred",
                    notes=[text],
                    files=file_paths,
                )
            continue  # skip hypothesis_candidates entirely

        elif item_type == "behavioral_guideline":
            # Store as a pending guideline candidate — surfaced separately, not via evidence loop.
            text = (item.get("text") or "").strip()
            if not text or not valid_cites:
                continue
            if trust_db.candidate_driver_exists(repo_root, session_id, driver):
                continue
            cid = trust_db.add_hypothesis_candidate(
                repo_root=repo_root,
                session_id=session_id,
                driver=driver,
                source="llm_generated",
                prompt=f"Save this as a coding style guideline: \"{text}\"",
                rationale=rationale + f"  (cites traces: {', '.join(str(t) for t in valid_cites)})",
                preference_json=json.dumps({"type": "behavioral_guideline", "text": text}),
                min_evidence=None,
            )
            # Behavioral guidelines surface on any valid citation — they're
            # LLM-observed facts about the codebase, not behavioral patterns
            # that need multiple confirming sessions to validate.
            trust_db.set_hypothesis_status(cid, "ready_to_surface")
            new_ids.append(cid)
            continue

        # else: existing preference handling — unchanged in behavior.
        hyp_prompt = (item.get("text") or item.get("prompt") or "").strip()
        if not hyp_prompt:
            continue
        if trust_db.candidate_driver_exists(repo_root, session_id, driver):
            continue

        pref = Preference(
            trigger=Trigger(stages=("apply",)),
            condition=Condition(),
            action=PreferenceAction.SOFT_CHECKIN,
            scope=Scope(level="repo"),
            lifecycle=Lifecycle(provenance="inferred_user_confirmed"),
        )
        # High-stakes flag raises the surfacing floor for this candidate.
        # Clamped: floor never drops below MIN_EVIDENCE, never above 2x.
        # The LLM cannot lower the bar — only raise it.
        high_stakes = bool(item.get("high_stakes"))
        candidate_min_evidence = MIN_EVIDENCE * 2 if high_stakes else None
        cid = trust_db.add_hypothesis_candidate(
            repo_root=repo_root,
            session_id=session_id,
            driver=driver,
            source="llm_generated",
            prompt=hyp_prompt,
            rationale=rationale + f"  (cites traces: {', '.join(str(t) for t in valid_cites)})",
            preference_json=json.dumps(preference_to_dict(pref)),
            min_evidence=candidate_min_evidence,
        )
        # Seed evidence_for with the citation count so candidates with enough
        # real cites can surface on the next update_evidence tick. Below the
        # floor, they accumulate like any other candidate. Respect this
        # candidate's own floor (high_stakes raises it to MIN_EVIDENCE*2) —
        # mirroring update_evidence's max(MIN_EVIDENCE, candidate_floor) — so a
        # high-stakes candidate can't shortcut-surface on MIN_EVIDENCE cites
        # when its declared bar is higher.
        surface_floor = candidate_min_evidence or MIN_EVIDENCE
        if len(valid_cites) > 0:
            trust_db.update_hypothesis_evidence(
                cid, delta_for=len(valid_cites), delta_against=0
            )
            if len(valid_cites) >= surface_floor:
                trust_db.set_hypothesis_status(cid, "ready_to_surface")
        new_ids.append(cid)

    return new_ids
