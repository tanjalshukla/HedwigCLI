"""
Q1: What predicts pushback?
Q2: What predicts failure_report specifically?
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import cross_val_score, StratifiedKFold
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier, export_text

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"

NUMERIC_FEATURES = [
    "turn_index",
    "session_position_frac",
    "is_first_turn",
    "is_continuation",
    "prompt_word_count",
    "prompt_char_count",
    "prev_tool_count",
    "prev_bash_count",
    "prev_write_edit_count",
    "prev_read_count",
    "prev_resp_word_count",
    "prev_has_ts_js",
    "prev_has_py",
    "prev_has_go",
    "prev_has_config",
    "prev_has_md",
    "prev_has_test",
    "time_since_prev_s",
    "cum_turn_index",
    "cum_pushback_count",
    "cum_failure_count",
    "cum_correction_count",
    "cum_distinct_files",
]

# Intent dummies — keep top categories
INTENT_CATS = ["create new code", "refactor", "debug", "understand", "test", "git", "connect", "other"]


def load_and_prep(annotated_only: bool = True) -> pd.DataFrame:
    df = pd.read_parquet(DATA / "features.parquet")
    if annotated_only:
        df = df[df["prompt_pushback"].notna()].copy()
    # Encode intent as dummies
    df["prompt_intent_clean"] = df["prompt_intent"].where(
        df["prompt_intent"].isin(INTENT_CATS), other="other"
    ).fillna("other")
    intent_dummies = pd.get_dummies(df["prompt_intent_clean"], prefix="intent")
    df = pd.concat([df, intent_dummies], axis=1)
    return df


def get_feature_cols(df: pd.DataFrame) -> list[str]:
    intent_cols = [c for c in df.columns if c.startswith("intent_")]
    return NUMERIC_FEATURES + intent_cols


def q1_any_pushback(df: pd.DataFrame) -> dict:
    print("\n=== Q1: Predicting any pushback ===")
    feat_cols = get_feature_cols(df)
    avail = [c for c in feat_cols if c in df.columns]

    X = df[avail].copy()
    # Fill missing with median
    for c in X.columns:
        if X[c].dtype in [float, np.float64]:
            X[c] = X[c].fillna(X[c].median())
        else:
            X[c] = X[c].fillna(0)
    y = df["pushback_is_any"]

    print(f"  Dataset: {len(X)} turns, {y.mean():.3f} base pushback rate")

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Logistic regression
    lr = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    auc_scores = cross_val_score(lr, X_scaled, y, cv=cv, scoring="roc_auc")
    print(f"  LR 5-fold AUC: {auc_scores.mean():.3f} ± {auc_scores.std():.3f}")

    lr.fit(X_scaled, y)
    coefs = pd.Series(lr.coef_[0], index=avail).abs().sort_values(ascending=False)
    signed = pd.Series(lr.coef_[0], index=avail).sort_values(key=abs, ascending=False)
    print("  Top 15 features by |coefficient|:")
    for feat, val in signed.head(15).items():
        direction = "↑ pushback" if val > 0 else "↓ pushback"
        print(f"    {feat:40s} {val:+.3f}  {direction}")

    # Decision tree for interpretability
    dt = DecisionTreeClassifier(max_depth=4, class_weight="balanced", random_state=42)
    dt_scores = cross_val_score(dt, X, y, cv=cv, scoring="roc_auc")
    print(f"  DT 5-fold AUC: {dt_scores.mean():.3f} ± {dt_scores.std():.3f}")
    dt.fit(X, y)
    fi = pd.Series(dt.feature_importances_, index=avail).sort_values(ascending=False)
    print("  Top 10 DT feature importances:")
    for feat, val in fi.head(10).items():
        print(f"    {feat:40s} {val:.4f}")

    return {
        "lr_auc_mean": round(float(auc_scores.mean()), 4),
        "lr_auc_std": round(float(auc_scores.std()), 4),
        "dt_auc_mean": round(float(dt_scores.mean()), 4),
        "base_rate": round(float(y.mean()), 4),
        "n_samples": int(len(y)),
        "top_lr_features": {k: round(float(v), 4) for k, v in signed.head(20).items()},
        "top_dt_features": {k: round(float(v), 4) for k, v in fi.head(20).items()},
    }


def q2_failure_report(df: pd.DataFrame) -> dict:
    print("\n=== Q2: Predicting failure_report ===")
    feat_cols = get_feature_cols(df)
    avail = [c for c in feat_cols if c in df.columns]

    X = df[avail].copy()
    for c in X.columns:
        if X[c].dtype in [float, np.float64]:
            X[c] = X[c].fillna(X[c].median())
        else:
            X[c] = X[c].fillna(0)
    y = df["is_failure_report"]

    print(f"  Dataset: {len(X)} turns, {y.mean():.3f} failure_report rate")
    print(f"  ({int(y.sum())} failure_report turns)")

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    lr = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    auc_scores = cross_val_score(lr, X_scaled, y, cv=cv, scoring="roc_auc")
    print(f"  LR 5-fold AUC: {auc_scores.mean():.3f} ± {auc_scores.std():.3f}")

    lr.fit(X_scaled, y)
    signed = pd.Series(lr.coef_[0], index=avail).sort_values(key=abs, ascending=False)
    print("  Top 15 features by |coefficient|:")
    for feat, val in signed.head(15).items():
        direction = "↑ failure_report" if val > 0 else "↓ failure_report"
        print(f"    {feat:40s} {val:+.3f}  {direction}")

    # Preceding tool patterns for failure_report turns
    fr_turns = df[df["is_failure_report"] == 1]
    print(f"\n  Preceding tool patterns for {len(fr_turns)} failure_report turns:")
    for col in ["prev_bash_count", "prev_write_edit_count", "prev_read_count", "prev_tool_count"]:
        m = fr_turns[col].mean()
        m_all = df[col].mean()
        print(f"    {col:35s}: FR mean={m:.2f}  all-turns mean={m_all:.2f}  ratio={m/max(m_all,0.001):.2f}x")

    print("  Preceding bash categories for failure_report turns:")
    bc = fr_turns["prev_bash_category"].value_counts(normalize=True)
    for cat, pct in bc.head(6).items():
        print(f"    {str(cat):20s}: {pct:.2%}")

    print("  Intent of failure_report turns:")
    ic = fr_turns["prompt_intent"].value_counts(normalize=True)
    for intent, pct in ic.head(6).items():
        print(f"    {str(intent):20s}: {pct:.2%}")

    return {
        "lr_auc_mean": round(float(auc_scores.mean()), 4),
        "lr_auc_std": round(float(auc_scores.std()), 4),
        "base_rate": round(float(y.mean()), 4),
        "n_failure_reports": int(y.sum()),
        "top_lr_features": {k: round(float(v), 4) for k, v in signed.head(20).items()},
        "prev_bash_cat_dist": bc.head(8).to_dict(),
        "intent_dist": ic.head(8).to_dict(),
        "prev_tool_means": {
            col: {"fr": round(float(fr_turns[col].mean()), 3),
                  "all": round(float(df[col].mean()), 3)}
            for col in ["prev_bash_count", "prev_write_edit_count", "prev_read_count", "prev_tool_count"]
        },
    }


def main() -> None:
    df = load_and_prep()
    q1 = q1_any_pushback(df)
    q2 = q2_failure_report(df)

    results = {"q1_any_pushback": q1, "q2_failure_report": q2}
    out = DATA / "pushback_model.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {out}")


if __name__ == "__main__":
    main()
