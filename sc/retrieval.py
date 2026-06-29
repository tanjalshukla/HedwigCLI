from __future__ import annotations

"""Retrieval seam — turns (query text, candidate texts) into similarity scores.

This is a real new concept that earns its own module rather than living in
rule_store.py: the three near-duplicate rankers in RuleStoreMixin
(`relevant_logic_notes`, `relevant_behavioral_guidelines`,
`relevant_feedback_snippets`) all reduced to one operation — "how related is
this candidate string to the query?" — but each open-coded the same
token-intersection (`_overlap_score`) inline with its own per-field weights.
Collapsing that single operation behind a seam lets the *relatedness* judgment
swap from lexical to semantic without touching the per-field weighting each
ranker keeps.

Two adapters satisfy the seam, mirroring the PolicyScorer pattern in
sc/policy.py (Protocol + adapters + a `select_*` function):

  KeywordRanker   — the existing `_overlap_score` token-intersection. Retained
                    as the always-available fallback; no third-party deps.
  EmbeddingRanker — fastembed (~30MB onnx, NO torch). Embeds query + candidate
                    text and ranks by cosine similarity, so a rule that says
                    "dependency injection" still matches a task that says
                    "constructor arguments". This is the DEFAULT.

This is semantic *matching*, not a learned policy — do not describe it as
"learned". The relatedness function changes; nothing about it is trained on
developer outcomes.

Graceful degradation is load-bearing: `select_ranker()` returns an
EmbeddingRanker only if fastembed is importable AND its model can be
materialized. If either fails (package missing, offline first-run fetch), it
silently returns a KeywordRanker — no crash, no error surfaced to the caller.
EmbeddingRanker also degrades per-call: any embed failure falls back to
keyword scoring for that call.
"""

import math
import os
import re
from typing import Protocol, Sequence


_RETRIEVAL_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "before", "by", "do", "for",
    "from", "if", "in", "into", "is", "it", "its", "of", "on", "or", "same",
    "should", "that", "the", "then", "this", "to", "use", "with",
}


def retrieval_tokens(*parts: str | None) -> set[str]:
    """Tokenize text into the retrieval vocabulary (lowercase, destopworded).

    Single source of truth for tokenization. rule_store imports this so the
    keyword path and the seam agree on tokens.
    """
    tokens: set[str] = set()
    for part in parts:
        if not part:
            continue
        for token in re.findall(r"[a-z0-9_]+", part.lower()):
            if len(token) < 3 or token in _RETRIEVAL_STOPWORDS:
                continue
            tokens.add(token)
    return tokens


def overlap_score(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    """Token-intersection relatedness — the historical `_overlap_score`."""
    if not query_tokens or not candidate_tokens:
        return 0.0
    score = 0.0
    for token in query_tokens & candidate_tokens:
        score += 1.5 if len(token) >= 7 else 1.0
    return score


class Retrieval(Protocol):
    """Seam for scoring candidate texts against a query.

    `score_candidates` returns one relatedness score per candidate, aligned by
    index. Callers keep their own per-field weighting and ranking; the seam
    only answers "how related is each candidate to the query?".
    """

    def score_candidates(
        self,
        query_text: str,
        candidates: Sequence[str],
    ) -> list[float]: ...


class KeywordRanker:
    """Token-intersection adapter — the cold-start / always-available path.

    Scores are calibrated to match the historical `_overlap_score` output so
    the keyword path is behavior-preserving.
    """

    def score_candidates(
        self,
        query_text: str,
        candidates: Sequence[str],
    ) -> list[float]:
        query_tokens = retrieval_tokens(query_text)
        return [
            overlap_score(query_tokens, retrieval_tokens(candidate))
            for candidate in candidates
        ]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class EmbeddingRanker:
    """fastembed adapter — cosine similarity over dense embeddings (DEFAULT).

    Holds an embedding callable (`embed_fn`) that maps a list of strings to a
    list of vectors. In production this wraps a fastembed model; tests inject a
    deterministic stub to exercise the cosine ranking logic without the model.

    Scores are scaled to the keyword range so callers' downstream thresholds
    (`score > 0`) and per-field weights behave consistently across adapters:
    cosine in [-1, 1] is mapped to [0, ~2] via max(0, cos) * scale, matching
    the "one strong token match" magnitude of overlap_score.
    """

    def __init__(self, embed_fn, *, scale: float = 2.0) -> None:
        self._embed_fn = embed_fn
        self._scale = scale
        self._keyword_fallback = KeywordRanker()

    def score_candidates(
        self,
        query_text: str,
        candidates: Sequence[str],
    ) -> list[float]:
        if not candidates:
            return []
        try:
            vectors = self._embed_fn([query_text, *list(candidates)])
            query_vec = vectors[0]
            candidate_vecs = vectors[1:]
            scores: list[float] = []
            for vec in candidate_vecs:
                cos = _cosine(query_vec, vec)
                scores.append(max(0.0, cos) * self._scale)
            return scores
        except Exception:
            # Per-call degradation: any embedding failure (model unloaded,
            # OOM, malformed input) falls back to keyword scoring rather than
            # crashing the prompt build.
            return self._keyword_fallback.score_candidates(query_text, candidates)


# Default fastembed model — small, onnx, no torch. Overridable via env if a
# deployment wants a different fastembed-supported model.
_DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# Module-level cache so we only attempt the (potentially slow / network)
# model materialization once per process. None = not yet attempted;
# False = attempted and failed (stay on keyword).
_EMBED_FN_CACHE: object | None = None


def _build_fastembed_fn():
    """Try to build a fastembed embedding callable.

    Returns a callable `list[str] -> list[list[float]]` on success, or None if
    fastembed is not importable or the model can't be materialized (e.g.
    offline first-run with no cached model). Never raises.
    """
    import os

    try:
        from fastembed import TextEmbedding  # type: ignore
    except Exception:
        return None

    model_name = os.environ.get("HEDWIG_EMBED_MODEL", _DEFAULT_EMBED_MODEL)
    try:
        model = TextEmbedding(model_name=model_name)
    except Exception:
        # Model download failed (offline) or unsupported model name.
        return None

    def _embed(texts: list[str]) -> list[list[float]]:
        # fastembed yields numpy arrays; convert to plain lists so nothing
        # downstream depends on numpy. (numpy arrives via fastembed/onnx,
        # never via sc.ml_policy — the dependency wall holds.)
        return [list(map(float, vec)) for vec in model.embed(texts)]

    return _embed


def select_ranker(*, prefer_embeddings: bool = True) -> tuple[Retrieval, str]:
    """Pick the active ranker and tag which one fired.

    Returns (ranker, label). Mirrors `select_scorer()` in sc/policy.py. The
    EmbeddingRanker is the default; it is chosen only if fastembed is
    importable and the model materializes. Otherwise we silently fall back to
    KeywordRanker — graceful degradation is part of the contract.

    HEDWIG_DISABLE_EMBEDDINGS=1 forces keyword ranking regardless. The plugin's
    per-prompt memory hook sets it: materializing the fastembed model costs
    ~5s on a cold process, and every hook invocation is a fresh subprocess (the
    cache below never warms across calls), so embeddings would add ~5s to every
    prompt. Keyword ranking is instant and, at plugin scale (a handful of
    guidelines), the quality difference is negligible. The long-lived CLI keeps
    embeddings (the cache warms once per session)."""
    global _EMBED_FN_CACHE

    if not prefer_embeddings or os.environ.get("HEDWIG_DISABLE_EMBEDDINGS"):
        return KeywordRanker(), "keyword"

    if _EMBED_FN_CACHE is None:
        _EMBED_FN_CACHE = _build_fastembed_fn() or False

    if _EMBED_FN_CACHE:
        return EmbeddingRanker(_EMBED_FN_CACHE), "embedding"
    return KeywordRanker(), "keyword"


def reset_ranker_cache() -> None:
    """Clear the process-level embedding-fn cache (test seam)."""
    global _EMBED_FN_CACHE
    _EMBED_FN_CACHE = None
