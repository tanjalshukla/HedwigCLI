"""
V3 Analysis E: Random forest + interaction effects.
Tests whether non-linear / conditional patterns exist beyond what LR captured.
"""
from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.model_selection import cross_val_score
from sklearn.preprocessing import StandardScaler

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"

RNG = np.random.default_rng(42)

FEATURE_COLS = [
    "is_first_turn", "is_continuation", "prompt_word_count", "prompt_char_count",
    "prev_tool_count", "prev_bash_count", "prev_read_count", "prev_write_edit_count",
    "prev_has_py", "prev_has_ts_js", "prev_has_config", "prev_has_md",
    "prev_bash_git", "prev_bash_test_build", "prev_bash_pkg", "prev_bash_file_ops",
    "cum_turn_index", "cum_pushback_count", "cum_correction_count",
    "intent_debug", "intent_refactor", "intent_create", "intent_test",
    "intent_understand", "intent_other",
]


def run_rf_analysis(df: pd.DataFrame, target_col: str, label: str) -> dict:
    available_cols = [c for c in FEATURE_COLS if c in df.columns]
    X = df[available_cols].fillna(0).values
    y = df[target_col].values.astype(int)

    print(f"\n{'='*60}")
    print(f"Random Forest: {label}")
    print(f"  {X.shape[0]} samples, {X.shape[1]} features, positive rate: {y.mean():.3f}")

    # 5-fold CV AUC
    rf = RandomForestClassifier(
        n_estimators=100, max_depth=None, random_state=42, n_jobs=-1
    )
    auc_scores = cross_val_score(rf, X, y, cv=5, scoring="roc_auc")
    mean_auc = float(np.mean(auc_scores))
    print(f"  5-fold AUC: {mean_auc:.4f} (std: {np.std(auc_scores):.4f})")

    # Fit on full data for feature importance
    rf.fit(X, y)

    # MDI (mean decrease impurity) importance
    mdi_importance = dict(zip(available_cols, rf.feature_importances_))

    # Permutation importance (more reliable for correlated features)
    print("  Computing permutation importance...")
    perm_result = permutation_importance(
        rf, X, y, n_repeats=10, random_state=42, n_jobs=-1, scoring="roc_auc"
    )
    perm_importance = dict(zip(available_cols, perm_result.importances_mean))

    # Top features by both methods
    mdi_sorted = sorted(mdi_importance.items(), key=lambda x: x[1], reverse=True)[:10]
    perm_sorted = sorted(perm_importance.items(), key=lambda x: x[1], reverse=True)[:10]

    print("  Top 5 (MDI):")
    for name, imp in mdi_sorted[:5]:
        print(f"    {name}: {imp:.4f}")
    print("  Top 5 (Permutation):")
    for name, imp in perm_sorted[:5]:
        print(f"    {name}: {imp:.4f}")

    # Check for interactions: train individual features vs pairs
    # If pair AUC >> single-feature AUC, interaction matters
    top_5_features = [name for name, _ in perm_sorted[:5]]
    interaction_test = {}
    for i, f1 in enumerate(top_5_features):
        f1_idx = available_cols.index(f1)
        X_single = X[:, f1_idx:f1_idx+1]
        rf_single = RandomForestClassifier(n_estimators=50, max_depth=5, random_state=42, n_jobs=-1)
        single_auc = float(np.mean(cross_val_score(rf_single, X_single, y, cv=3, scoring="roc_auc")))

        for f2 in top_5_features[i+1:]:
            f2_idx = available_cols.index(f2)
            X_pair = X[:, [f1_idx, f2_idx]]
            rf_pair = RandomForestClassifier(n_estimators=50, max_depth=5, random_state=42, n_jobs=-1)
            pair_auc = float(np.mean(cross_val_score(rf_pair, X_pair, y, cv=3, scoring="roc_auc")))

            interaction_test[f"{f1} × {f2}"] = {
                "single_auc_f1": round(single_auc, 4),
                "pair_auc": round(pair_auc, 4),
                "gain": round(pair_auc - single_auc, 4),
            }

    # Sort interactions by gain
    interactions_sorted = sorted(interaction_test.items(), key=lambda x: x[1]["gain"], reverse=True)
    print("  Top interaction pairs (pair AUC - single AUC):")
    for pair_name, info in interactions_sorted[:5]:
        print(f"    {pair_name}: gain={info['gain']:.4f} (single={info['single_auc_f1']:.4f}, pair={info['pair_auc']:.4f})")

    return {
        "label": label,
        "auc_mean": round(mean_auc, 4),
        "auc_std": round(float(np.std(auc_scores)), 4),
        "mdi_importance": {k: round(v, 5) for k, v in mdi_sorted},
        "perm_importance": {k: round(v, 5) for k, v in perm_sorted},
        "interaction_tests": dict(interactions_sorted[:10]),
    }


def main() -> None:
    print("Loading features...")
    df = pd.read_parquet(DATA / "features.parquet")
    print(f"  {df.shape[0]} rows, {df.shape[1]} cols")

    # Q1: Pushback prediction
    q1_results = run_rf_analysis(df, "pushback_is_any", "Pushback (any)")

    # Q2: Failure report prediction
    q2_results = run_rf_analysis(df, "is_failure_report", "Failure Report")

    # Compare to LR baselines
    print(f"\n{'='*60}")
    print("Comparison to LR baselines from v2:")
    print(f"  Pushback: LR AUC=0.754, RF AUC={q1_results['auc_mean']:.4f} (Δ={q1_results['auc_mean']-0.754:+.4f})")
    print(f"  Failure:  LR AUC=0.897, RF AUC={q2_results['auc_mean']:.4f} (Δ={q2_results['auc_mean']-0.897:+.4f})")

    output = {
        "pushback": q1_results,
        "failure_report": q2_results,
        "comparison": {
            "pushback_lr_auc": 0.754,
            "pushback_rf_auc": q1_results["auc_mean"],
            "pushback_delta": round(q1_results["auc_mean"] - 0.754, 4),
            "failure_lr_auc": 0.897,
            "failure_rf_auc": q2_results["auc_mean"],
            "failure_delta": round(q2_results["auc_mean"] - 0.897, 4),
        },
    }

    out_path = DATA / "rf_importance.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
