"""
Q3: Does behavior cluster into 3-4 personas?
K-means on session-level behavioral features, k=2..6.
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"

SESSION_FEATURES = [
    "pushback_rate",
    "failure_rate",
    "correction_rate",
    "mean_prompt_words",
    "mean_prev_tools",
    "mean_prev_bash",
    "mean_prev_write",
    "n_turns",
    "files_touched_count",
    "action_count",
    "research_count",
]

OPTIONAL = ["mean_time_between_s", "agent_percentage"]


def main() -> None:
    df = pd.read_parquet(DATA / "session_features.parquet")
    print(f"Loaded {len(df)} sessions")

    feat_cols = SESSION_FEATURES.copy()
    for c in OPTIONAL:
        if c in df.columns:
            feat_cols.append(c)

    X_raw = df[feat_cols].copy()
    for c in X_raw.columns:
        X_raw[c] = pd.to_numeric(X_raw[c], errors="coerce")
        X_raw[c] = X_raw[c].fillna(X_raw[c].median())

    scaler = StandardScaler()
    X = scaler.fit_transform(X_raw)

    print("\nK-means sweep k=2..6:")
    results = {}
    for k in range(2, 7):
        km = KMeans(n_clusters=k, random_state=42, n_init=10)
        labels = km.fit_predict(X)
        sil = silhouette_score(X, labels)
        inertia = km.inertia_
        counts = pd.Series(labels).value_counts().sort_index().to_dict()
        print(f"  k={k}: silhouette={sil:.4f}  inertia={inertia:.1f}  sizes={counts}")
        results[k] = {"silhouette": round(sil, 4), "inertia": round(inertia, 1), "sizes": counts}

    # Use k=4 as primary (matches our assumed persona count)
    best_k = max(results, key=lambda k: results[k]["silhouette"])
    print(f"\nBest silhouette at k={best_k}")

    for k_analyze in [best_k, 4]:
        print(f"\n--- Cluster analysis k={k_analyze} ---")
        km = KMeans(n_clusters=k_analyze, random_state=42, n_init=10)
        labels = km.fit_predict(X)
        df[f"cluster_k{k_analyze}"] = labels

        # Centroids in original scale
        centroids_scaled = km.cluster_centers_
        centroids = scaler.inverse_transform(centroids_scaled)
        centroid_df = pd.DataFrame(centroids, columns=feat_cols)

        for cl in range(k_analyze):
            mask = labels == cl
            sub = df[mask]
            c_row = centroid_df.iloc[cl]
            print(f"\n  Cluster {cl} (n={mask.sum()}):")
            print(f"    pushback_rate={c_row['pushback_rate']:.3f}  "
                  f"failure_rate={c_row['failure_rate']:.3f}  "
                  f"correction_rate={c_row['correction_rate']:.3f}")
            print(f"    mean_prompt_words={c_row['mean_prompt_words']:.1f}  "
                  f"mean_prev_tools={c_row['mean_prev_tools']:.2f}  "
                  f"n_turns={c_row['n_turns']:.1f}")
            if "agent_percentage" in feat_cols:
                print(f"    agent_pct={c_row['agent_percentage']:.1f}")
            # vs SWE-chat persona labels
            if "user_persona" in sub.columns:
                persona_dist = sub["user_persona"].value_counts(normalize=True)
                print(f"    SWE-chat persona dist: {persona_dist.to_dict()}")

    # Save cluster assignments + centroid data
    cluster_out = {}
    for k_analyze in sorted(set([best_k, 4])):
        km = KMeans(n_clusters=k_analyze, random_state=42, n_init=10)
        labels = km.fit_predict(X)
        centroids = scaler.inverse_transform(km.cluster_centers_)
        centroid_df = pd.DataFrame(centroids, columns=feat_cols)

        df_labeled = df.copy()
        df_labeled["cluster"] = labels

        cluster_summaries = []
        for cl in range(k_analyze):
            sub = df_labeled[df_labeled["cluster"] == cl]
            persona_dist = {}
            if "user_persona" in sub.columns:
                persona_dist = sub["user_persona"].value_counts().to_dict()
            cluster_summaries.append({
                "cluster_id": cl,
                "n": int((labels == cl).sum()),
                "centroid": {c: round(float(centroid_df.iloc[cl][c]), 4) for c in feat_cols},
                "swe_chat_persona_dist": persona_dist,
            })

        cluster_out[f"k{k_analyze}"] = {
            "silhouette": results[k_analyze]["silhouette"],
            "clusters": cluster_summaries,
        }

    out_path = DATA / "clusters.json"
    with open(out_path, "w") as f:
        json.dump({"sweep": results, "best_k": best_k, "detail": cluster_out}, f, indent=2)
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
