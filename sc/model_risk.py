from __future__ import annotations

"""Adversarial-reviewer risk signal — advisory augmentation of RiskSignals.

A second model pass with a *different* system prompt from the agent. The
reviewer is shown only the file path, the diff (or new file content), and a
short slice of the surrounding file context. It does NOT see the agent's
intent declaration or any prior agent reasoning — the framing is "you are a
code reviewer flagging risk; you have not seen the author's reasoning."

Output is strictly advisory:
    - ``model_risk_score`` in [0, 1]: 0.0 = looks safe, 1.0 = high risk.
    - ``model_risk_rationale``: short string surfaced in the apply panel.

On any failure (Bedrock error, JSON parse failure, schema validation
failure, score out of range, timeout) we return ``(0.5, "")`` — i.e. "no
opinion" — so the deterministic signals from ``features.assess_risk`` stay
authoritative. The hardcoded signals (change_pattern, blast_radius,
is_security_sensitive, is_new_file, diff_size) are never replaced; this
function only augments.

Caching: in-process, keyed by ``(file_path, sha256(diff_or_content))``.
No persistence — the cache resets per CLI invocation, which is fine for
the MVP since a given (path, content) pair is reviewed at most once per
apply turn.
"""

import hashlib
import json
from typing import Any

# Hard upper bound on file_context characters fed to the reviewer. The
# reviewer benefits from seeing surrounding code but does not need the
# whole file — and Bedrock latency scales with input size. 4_000 chars is
# roughly 60-100 lines of typical Python source, enough to recognize
# touched call sites without making this a long-context request.
_FILE_CONTEXT_CHAR_LIMIT = 4_000

# Hard upper bound on diff_or_content characters. Same reasoning.
_DIFF_CHAR_LIMIT = 8_000

_REVIEWER_SYSTEM_PROMPT = (
    "You are a senior code reviewer. You have NOT seen the author's "
    "reasoning, plan, or intent — only the file, the diff, and a short "
    "slice of surrounding context. Your job is to flag risk in the "
    "proposed change. Be skeptical but specific. Return JSON only."
)

_REVIEWER_PROMPT_PREFIX = (
    "Return JSON only matching this schema:\n"
    '{"score": float in [0.0, 1.0], "rationale": "short string"}\n\n'
    "score = 0.0  -> change looks safe and well-scoped.\n"
    "score = 1.0  -> change is dangerous, ill-scoped, or likely to break "
    "things.\n"
    "rationale: one sentence (<=20 words) explaining the score.\n\n"
)


def _build_user_prompt(file_path: str, file_context: str, diff_or_content: str) -> str:
    """Assemble the reviewer user prompt without ``str.format`` — the
    schema literal contains JSON braces which would collide with format()."""
    return (
        _REVIEWER_PROMPT_PREFIX
        + f"File: {file_path}\n\n"
        + "Surrounding file context:\n-----\n"
        + (file_context or "(none)")
        + "\n-----\n\n"
        + "Proposed change (diff or new file content):\n-----\n"
        + (diff_or_content or "(empty)")
        + "\n-----\n"
    )

# Module-level cache. Bounded informally by the number of distinct
# (path, sha) pairs seen during one CLI run. We do not evict — the
# session lifetime is the eviction policy.
_CACHE: dict[tuple[str, str], tuple[float, str]] = {}


def _cache_key(file_path: str, diff_or_content: str) -> tuple[str, str]:
    sha = hashlib.sha256(diff_or_content.encode("utf-8", errors="replace")).hexdigest()
    return (file_path, sha)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit] + "\n…(truncated)"


def _validate_payload(payload: Any) -> tuple[float, str] | None:
    """Strict schema check. Returns (score, rationale) or None on any failure."""
    if not isinstance(payload, dict):
        return None
    score = payload.get("score")
    rationale = payload.get("rationale")
    if not isinstance(score, (int, float)):
        return None
    if isinstance(score, bool):  # bool is an int subclass; reject it
        return None
    score_f = float(score)
    if not (0.0 <= score_f <= 1.0):
        return None
    if not isinstance(rationale, str):
        return None
    rationale = rationale.strip()
    # Empty rationale is a tell-tale sign of a low-effort response. Treat
    # as a soft schema failure — fall back to "no opinion".
    if not rationale:
        return None
    return score_f, rationale


def assess_risk_via_model(
    file_path: str,
    diff_or_content: str,
    file_context: str,
    agent_client: Any,
) -> tuple[float, str]:
    """Adversarial-reviewer risk pass. Advisory only.

    Returns ``(score, rationale)``. On any failure returns ``(0.5, "")``.

    ``agent_client`` is duck-typed: anything exposing ``client.messages.create``
    (the AnthropicBedrock surface used by ``ClaudeClient``) works. We deliberately
    do NOT route through ``ClaudeClient.declare_intent`` etc. — those methods
    share the agent's session, and the reviewer must be a fresh, independent
    pass with its own system prompt.
    """
    if agent_client is None:
        return 0.5, ""

    diff_or_content = _truncate(diff_or_content, _DIFF_CHAR_LIMIT)
    file_context = _truncate(file_context, _FILE_CONTEXT_CHAR_LIMIT)

    key = _cache_key(file_path, diff_or_content)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    user_prompt = _build_user_prompt(file_path, file_context, diff_or_content)

    try:
        # Use the underlying Bedrock client directly — different system prompt,
        # no session reuse, no retries beyond what AnthropicBedrock does
        # internally. We treat any exception as "no opinion".
        client = getattr(agent_client, "client", None)
        model_id = getattr(agent_client, "model_id", None)
        if client is None or model_id is None:
            return 0.5, ""
        response = client.messages.create(
            model=model_id,
            max_tokens=200,
            temperature=0.0,
            system=_REVIEWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception:
        return 0.5, ""

    raw = _extract_text(response)
    if not raw:
        return 0.5, ""

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Best-effort: pull the first {...} substring and try again. Keeps
        # the path tolerant of minor preamble without loosening validation
        # of the parsed object.
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return 0.5, ""
        try:
            payload = json.loads(raw[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            return 0.5, ""

    validated = _validate_payload(payload)
    if validated is None:
        return 0.5, ""

    _CACHE[key] = validated
    return validated


def _extract_text(response: Any) -> str:
    """Pull text out of an AnthropicBedrock response. Tolerant of dict/object forms."""
    try:
        content = getattr(response, "content", None)
        if content is None and isinstance(response, dict):
            content = response.get("content")
        if not isinstance(content, list):
            return ""
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict):
                chunks.append(block.get("text", "") or "")
                continue
            text = getattr(block, "text", None)
            chunks.append(text or "")
        return "".join(chunks)
    except Exception:
        return ""


def _reset_cache_for_tests() -> None:
    """Test hook — never call from production code."""
    _CACHE.clear()
