"""Tests for the Retrieval seam (sc/retrieval.py) and its rule_store wiring.

Covers:
  - KeywordRanker reproduces the historical _overlap_score behavior (no
    regression on the keyword path).
  - EmbeddingRanker finds a semantic-but-non-lexical match where keyword
    overlap returns nothing — exercising the real cosine ranking logic with a
    deterministic embedder stub (fastembed is not assumed present in CI).
  - select_ranker() degrades gracefully to KeywordRanker when fastembed is
    missing or its model can't be fetched — no raise, keyword results returned.
  - The three collapsed rankers in RuleStoreMixin return the same shapes as
    before and benefit from the seam.
"""

import tempfile
import unittest
from pathlib import Path

from sc import retrieval
from sc.retrieval import (
    EmbeddingRanker,
    KeywordRanker,
    overlap_score,
    retrieval_tokens,
    select_ranker,
)
from sc.trust_db import TrustDB


# --- A deterministic embedder stub ---------------------------------------
#
# Maps each text to a small "concept" vector by counting concept-keyword hits.
# "dependency injection" and "constructor arguments" both map onto the same
# concept axis, so cosine similarity between them is high even though they
# share NO surface tokens. This exercises the cosine math, not a trivial pass.

_CONCEPTS = {
    "di": ("dependency", "injection", "inject", "constructor", "wire", "wiring", "provide"),
    "auth": ("auth", "login", "password", "token", "credential", "session"),
    "cache": ("cache", "memoize", "ttl", "evict", "invalidate"),
}


def _concept_embed(texts):
    vectors = []
    for text in texts:
        low = text.lower()
        vec = []
        for _name, keywords in _CONCEPTS.items():
            vec.append(float(sum(low.count(kw) for kw in keywords)))
        # small constant so all-zero texts don't divide by zero in cosine
        vec.append(0.01)
        vectors.append(vec)
    return vectors


class KeywordRankerTests(unittest.TestCase):
    def test_matches_historical_overlap_score(self) -> None:
        ranker = KeywordRanker()
        query = "improve API error handling with explicit codes"
        candidates = [
            "Use AppError with explicit error codes for API failures.",
            "Prefer cursor pagination for list endpoints.",
        ]
        seam_scores = ranker.score_candidates(query, candidates)

        qt = retrieval_tokens(query)
        expected = [overlap_score(qt, retrieval_tokens(c)) for c in candidates]
        self.assertEqual(seam_scores, expected)
        # the error-handling candidate must outrank the pagination one
        self.assertGreater(seam_scores[0], seam_scores[1])

    def test_empty_candidates(self) -> None:
        self.assertEqual(KeywordRanker().score_candidates("q", []), [])


class EmbeddingRankerTests(unittest.TestCase):
    def test_semantic_match_without_lexical_overlap(self) -> None:
        ranker = EmbeddingRanker(_concept_embed)
        # query and the winning candidate share ZERO surface tokens
        query = "how should we pass constructor arguments when wiring objects"
        candidates = [
            "Use dependency injection to provide collaborators.",  # semantic match
            "Always evict the cache on writes.",  # unrelated
        ]

        # keyword path finds nothing for the semantic candidate
        kw = KeywordRanker().score_candidates(query, candidates)
        self.assertEqual(kw[0], 0.0)

        # embedding path ranks the semantic match first with a positive score
        emb = ranker.score_candidates(query, candidates)
        self.assertGreater(emb[0], 0.0)
        self.assertGreater(emb[0], emb[1])

    def test_per_call_fallback_on_embed_failure(self) -> None:
        def _boom(_texts):
            raise RuntimeError("model unloaded")

        ranker = EmbeddingRanker(_boom)
        query = "explicit error codes"
        candidates = ["Use explicit error codes everywhere.", "unrelated text"]
        scores = ranker.score_candidates(query, candidates)
        # degrades to keyword scoring rather than raising
        expected = KeywordRanker().score_candidates(query, candidates)
        self.assertEqual(scores, expected)


class SelectRankerTests(unittest.TestCase):
    def setUp(self) -> None:
        retrieval.reset_ranker_cache()
        self.addCleanup(retrieval.reset_ranker_cache)

    def test_prefer_embeddings_false_returns_keyword(self) -> None:
        ranker, label = select_ranker(prefer_embeddings=False)
        self.assertIsInstance(ranker, KeywordRanker)
        self.assertEqual(label, "keyword")

    def test_graceful_fallback_when_fastembed_missing(self) -> None:
        # Force the model-build to fail (simulates missing fastembed / offline
        # model fetch). select_ranker must NOT raise and must return keyword.
        original = retrieval._build_fastembed_fn
        retrieval._build_fastembed_fn = lambda: None  # type: ignore
        try:
            ranker, label = select_ranker()
        finally:
            retrieval._build_fastembed_fn = original  # type: ignore
        self.assertIsInstance(ranker, KeywordRanker)
        self.assertEqual(label, "keyword")
        # and it still produces usable keyword results
        scores = ranker.score_candidates("error codes", ["use error codes"])
        self.assertGreater(scores[0], 0.0)

    def test_embedding_selected_when_model_available(self) -> None:
        original = retrieval._build_fastembed_fn
        retrieval._build_fastembed_fn = lambda: _concept_embed  # type: ignore
        try:
            ranker, label = select_ranker()
        finally:
            retrieval._build_fastembed_fn = original  # type: ignore
        self.assertIsInstance(ranker, EmbeddingRanker)
        self.assertEqual(label, "embedding")


class RuleStoreSeamShapeTests(unittest.TestCase):
    """The collapsed rankers must keep their return shapes (no regression)."""

    def test_keyword_path_shapes_and_ranking(self) -> None:
        retrieval.reset_ranker_cache()
        self.addCleanup(retrieval.reset_ranker_cache)
        # force keyword path so this is deterministic regardless of fastembed
        _orig_build = retrieval._build_fastembed_fn
        retrieval._build_fastembed_fn = lambda: None  # type: ignore
        self.addCleanup(
            lambda: setattr(retrieval, "_build_fastembed_fn", _orig_build)
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.replace_behavioral_guidelines(
                repo_root=repo,
                source="rules.md",
                guidelines=[
                    "Do not change response envelopes without approval.",
                    "Always run tests after editing demo repo files.",
                ],
            )
            guidelines = db.relevant_behavioral_guidelines(
                repo,
                query_text="Add API filtering while preserving the response envelope.",
                limit=2,
            )
            self.assertGreaterEqual(len(guidelines), 1)
            self.assertEqual(
                guidelines[0].guideline,
                "Do not change response envelopes without approval.",
            )

            db.add_logic_notes(
                repo,
                source="run_summary",
                notes=[
                    "Added a dedicated summary endpoint instead of changing the existing list response envelope.",
                    "Validation changes in the service layer should stay local.",
                ],
                files=["task_api/api.py"],
                change_types=["api_change"],
            )
            notes = db.relevant_logic_notes(
                repo,
                query_text="Add a summary route while keeping the existing list response envelope intact.",
                limit=2,
            )
            self.assertGreaterEqual(len(notes), 1)
            self.assertIn("summary endpoint", notes[0].note)

    def test_embedding_path_semantic_feedback_match(self) -> None:
        """With an embedding ranker injected, a semantic-but-non-lexical
        feedback snippet is retrieved where keyword overlap would miss it."""
        retrieval.reset_ranker_cache()
        self.addCleanup(retrieval.reset_ranker_cache)
        _orig = retrieval._build_fastembed_fn
        retrieval._build_fastembed_fn = lambda: _concept_embed  # type: ignore
        self.addCleanup(lambda: setattr(retrieval, "_build_fastembed_fn", _orig))

        with tempfile.TemporaryDirectory() as tmpdir:
            db = TrustDB(Path(tmpdir) / "trust.db")
            repo = "/tmp/repo"

            db.record_trace(
                repo_root=repo,
                session_id="s1",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="src/wiring.py",
                change_type="refactor",
                diff_size=8,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.6,
                user_decision="deny",
                user_feedback_text="Use dependency injection to provide collaborators.",
            )
            db.record_trace(
                repo_root=repo,
                session_id="s2",
                task="task",
                stage="apply",
                action_type="write_request",
                file_path="src/cache.py",
                change_type="refactor",
                diff_size=4,
                blast_radius=1,
                existing_lease=False,
                lease_type=None,
                prior_approvals=0,
                prior_denials=0,
                policy_action="check_in",
                policy_score=-0.2,
                user_decision="approve",
                user_feedback_text="Always invalidate the cache on writes.",
            )

            # query shares no surface tokens with the winning snippet
            snippets = db.relevant_feedback_snippets(
                repo,
                query_text="how to pass constructor arguments and wire collaborators",
                limit=2,
            )
            self.assertGreaterEqual(len(snippets), 1)
            self.assertEqual(
                snippets[0], "Use dependency injection to provide collaborators."
            )


if __name__ == "__main__":
    unittest.main()
