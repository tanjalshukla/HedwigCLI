"""
Q5: What do developers actually push back about?
TF-IDF + keyword clustering on pushback turn content.
"""

from __future__ import annotations

import json
import pathlib
import re
from collections import Counter, defaultdict

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"

CATEGORY_KEYWORDS = {
    "content_correction": [
        "wrong", "incorrect", "mistake", "not right", "that's not", "should be",
        "needs to be", "instead", "actually", "no,", "nope", "change", "update",
        "replace", "fix", "correct",
    ],
    "approach_correction": [
        "different approach", "another way", "don't use", "avoid", "instead of",
        "refactor", "rewrite", "simplify", "clean", "cleaner", "better approach",
        "prefer", "use X instead", "let's", "we should",
    ],
    "timing_pacing": [
        "wait", "stop", "hold on", "not yet", "too fast", "slow down", "pause",
        "before you", "first let me", "let me", "one thing at a time",
    ],
    "style_formatting": [
        "format", "style", "indent", "whitespace", "naming", "camelCase", "snake_case",
        "comment", "documentation", "doc", "prettier", "lint",
    ],
    "failure_error": [
        "error", "fail", "failed", "broke", "broken", "crash", "exception",
        "traceback", "doesn't work", "not working", "issue", "bug", "problem",
        "throws", "TypeError", "undefined", "null",
    ],
    "clarity_understanding": [
        "what do you mean", "explain", "unclear", "confused", "don't understand",
        "can you clarify", "what is", "why did", "why are you", "i meant",
    ],
    "scope_requirements": [
        "only", "just", "don't touch", "leave", "keep", "don't change",
        "out of scope", "not part of", "focus on", "stick to",
    ],
}


def assign_category(text: str) -> str:
    """Assign the most-matched category to a text snippet."""
    if not text:
        return "other"
    text_low = text.lower()
    scores = {}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        scores[cat] = sum(1 for kw in keywords if kw in text_low)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


def main() -> None:
    df = pd.read_parquet(DATA / "features.parquet")

    # Restrict to pushback turns with actual content
    pushback_turns = df[
        df["prompt_pushback"].isin({"correction", "rejection", "failure_report"}) &
        df["prompt_word_count"].gt(2)
    ].copy()

    # We need the raw content — reload from sessions JSONL
    # Use what we stored during extraction
    sessions_dir = HERE / "data" / "swechat" / "sessions"
    if not sessions_dir.exists():
        print("No sessions JSONL found; cannot extract raw feedback text.")
        print("Re-using prompt_word_count as proxy — topic analysis skipped.")
        return

    print(f"Loading pushback text from {sessions_dir}...")
    texts = []
    meta = []
    import os
    for fname in sorted(os.listdir(sessions_dir))[:]:  # all sessions
        fpath = sessions_dir / fname
        try:
            with open(fpath) as f:
                for line in f:
                    row = json.loads(line.strip())
                    pb = row.get("_swechat_pushback") or row.get("prompt_pushback")
                    if pb in {"correction", "rejection", "failure_report"}:
                        text = (row.get("user_feedback_text") or row.get("task") or "").strip()
                        if text and len(text.split()) > 3:
                            texts.append(text)
                            meta.append({
                                "session_id": row["session_id"],
                                "pushback_type": pb,
                                "text": text[:500],
                            })
        except Exception:
            continue

    print(f"  {len(texts)} pushback texts loaded")
    if len(texts) < 50:
        print("  Too few texts for meaningful analysis.")
        return

    # TF-IDF
    vectorizer = TfidfVectorizer(
        max_features=500,
        ngram_range=(1, 2),
        stop_words="english",
        min_df=5,
        max_df=0.85,
    )
    X = vectorizer.fit_transform(texts)
    feature_names = vectorizer.get_feature_names_out()

    # Top terms overall
    mean_tfidf = np.asarray(X.mean(axis=0)).flatten()
    top_idx = mean_tfidf.argsort()[::-1][:30]
    top_terms = [(feature_names[i], round(float(mean_tfidf[i]), 4)) for i in top_idx]
    print("\nTop 30 TF-IDF terms across pushback turns:")
    for term, score in top_terms:
        print(f"  {term:30s} {score:.4f}")

    # Top terms per pushback type
    by_type: dict[str, list[str]] = defaultdict(list)
    for text, m in zip(texts, meta):
        by_type[m["pushback_type"]].append(text)

    type_top_terms = {}
    for pt, pt_texts in by_type.items():
        Xt = vectorizer.transform(pt_texts)
        mt = np.asarray(Xt.mean(axis=0)).flatten()
        top = [(feature_names[i], round(float(mt[i]), 4))
               for i in mt.argsort()[::-1][:20]]
        print(f"\nTop terms for {pt}:")
        for t, s in top:
            print(f"  {t:30s} {s:.4f}")
        type_top_terms[pt] = top

    # Keyword category assignment
    cats = [assign_category(t) for t in texts]
    cat_counts = Counter(cats)
    total = len(cats)
    print("\nKeyword category distribution:")
    for cat, cnt in sorted(cat_counts.items(), key=lambda x: -x[1]):
        pct = 100 * cnt / total
        print(f"  {cat:30s}: {cnt:5d} ({pct:.1f}%)")

    # Verbatim examples per category (top 3 each)
    cat_examples: dict[str, list[str]] = defaultdict(list)
    for text, cat in zip(texts, cats):
        cat_examples[cat].append(text)

    verbatims = {}
    for cat in sorted(cat_counts, key=lambda x: -cat_counts[x]):
        examples = cat_examples[cat][:3]
        verbatims[cat] = [e[:200] for e in examples]

    print("\nVerbatim examples per category:")
    for cat, exs in verbatims.items():
        print(f"\n  [{cat}]")
        for ex in exs:
            print(f"    » {ex[:120]}")

    out = {
        "n_pushback_texts": len(texts),
        "by_pushback_type": {k: len(v) for k, v in by_type.items()},
        "top_30_terms": top_terms,
        "per_type_top_terms": type_top_terms,
        "category_distribution": {k: {"count": v, "pct": round(100*v/total, 1)}
                                   for k, v in sorted(cat_counts.items(), key=lambda x: -x[1])},
        "verbatim_examples": verbatims,
    }
    out_path = DATA / "feedback_topics.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
