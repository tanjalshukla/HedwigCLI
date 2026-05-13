"""
V3 Analysis A: Re-characterize the 33% unclassified pushback bucket.
Encodes messages with sentence-transformers, clusters, labels clusters.
"""
from __future__ import annotations

import json
import os
import pathlib

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"
SESSIONS_DIR = DATA / "swechat" / "sessions"

# The v2 category keywords used to assign categories. We need the "other"
# messages — those that didn't match any keyword list.
CATEGORY_KEYWORDS = {
    "content_correction": [
        "wrong", "incorrect", "mistake", "not right", "that's not", "should be",
        "needs to be", "instead", "actually", "no,", "nope", "change", "update",
        "replace", "fix", "correct",
    ],
    "approach_correction": [
        "different approach", "another way", "don't use", "avoid", "instead of",
        "refactor", "rewrite", "simplify", "clean", "cleaner", "better approach",
        "prefer", "let's", "we should",
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
        "don't modify", "scope", "limit", "restrict",
    ],
}


def _matches_any_category(text: str) -> bool:
    text_lower = text.lower()
    for keywords in CATEGORY_KEYWORDS.values():
        if any(kw in text_lower for kw in keywords):
            return True
    return False


def load_other_messages() -> list[dict]:
    """Load pushback texts that didn't match any v2 keyword category."""
    msgs = []
    for fname in sorted(os.listdir(SESSIONS_DIR)):
        fpath = SESSIONS_DIR / fname
        try:
            with open(fpath) as f:
                for line in f:
                    row = json.loads(line.strip())
                    pb = row.get("_swechat_pushback") or row.get("prompt_pushback")
                    if pb not in {"correction", "rejection", "failure_report"}:
                        continue
                    text = (row.get("user_feedback_text") or row.get("task") or "").strip()
                    if not text or len(text.split()) <= 3:
                        continue
                    if _matches_any_category(text):
                        continue
                    msgs.append({
                        "session_id": row.get("session_id", ""),
                        "pushback_type": pb,
                        "text": text[:500],
                    })
        except Exception:
            continue
    return msgs


def main() -> None:
    print("Loading unclassified ('other') pushback messages...")
    msgs = load_other_messages()
    print(f"  {len(msgs)} messages loaded")

    if len(msgs) < 100:
        print("  Too few messages for meaningful clustering.")
        return

    texts = [m["text"] for m in msgs]

    print("Encoding with all-MiniLM-L6-v2...")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    embeddings = model.encode(texts, show_progress_bar=True, batch_size=256)
    embeddings = np.array(embeddings)
    print(f"  Embeddings shape: {embeddings.shape}")

    # Try k = 3, 5, 7, 10
    results = {}
    best_k = 5
    best_sil = -1
    for k in [3, 5, 7, 10]:
        km = KMeans(n_clusters=k, random_state=42, n_init=10)
        labels = km.fit_predict(embeddings)
        sil = silhouette_score(embeddings, labels, sample_size=min(5000, len(texts)))
        results[k] = {"silhouette": round(sil, 4)}
        print(f"  k={k}: silhouette={sil:.4f}")
        if sil > best_sil:
            best_sil = sil
            best_k = k

    print(f"\nBest k={best_k} (silhouette={best_sil:.4f})")

    # Final clustering at best k
    km = KMeans(n_clusters=best_k, random_state=42, n_init=10)
    labels = km.fit_predict(embeddings)

    # For each cluster: 5 closest-to-centroid + 5 random
    cluster_info = []
    for c in range(best_k):
        mask = labels == c
        cluster_size = int(mask.sum())
        cluster_embeddings = embeddings[mask]
        cluster_texts = [texts[i] for i in range(len(texts)) if labels[i] == c]

        # Closest to centroid
        centroid = km.cluster_centers_[c]
        dists = np.linalg.norm(cluster_embeddings - centroid, axis=1)
        closest_idx = np.argsort(dists)[:5]
        closest_verbatims = [cluster_texts[i][:300] for i in closest_idx]

        # Random sample
        rng = np.random.default_rng(42 + c)
        random_idx = rng.choice(cluster_size, size=min(5, cluster_size), replace=False)
        random_verbatims = [cluster_texts[i][:300] for i in random_idx]

        cluster_info.append({
            "cluster_id": c,
            "size": cluster_size,
            "pct_of_total": round(100 * cluster_size / len(texts), 1),
            "closest_to_centroid": closest_verbatims,
            "random_sample": random_verbatims,
            "label": "",  # to be filled after inspection
        })

    output = {
        "n_messages": len(texts),
        "silhouette_scores": results,
        "best_k": best_k,
        "best_silhouette": best_sil,
        "clusters": cluster_info,
    }

    out_path = DATA / "pushback_other_clusters.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {out_path}")
    print("\nCluster summaries (inspect verbatims to assign labels):")
    for info in cluster_info:
        print(f"\n  Cluster {info['cluster_id']} (n={info['size']}, {info['pct_of_total']}%)")
        print(f"    Closest: {info['closest_to_centroid'][0][:80]}...")
        print(f"    Random:  {info['random_sample'][0][:80]}...")


if __name__ == "__main__":
    main()
