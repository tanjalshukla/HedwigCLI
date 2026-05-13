"""
V3 Analysis B: Bootstrap confidence intervals on headline findings.
500 resamples on LR coefficients (Q1, Q2) and ICC (Q4).
"""
from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"

RNG = np.random.default_rng(42)
N_BOOTSTRAP = 500

# Same features used in v2 predict_pushback.py
FEATURE_COLS = [
    "is_first_turn", "is_continuation", "prompt_word_count", "prompt_char_count",
    "prev_tool_count", "prev_bash_count", "prev_read_count", "prev_write_edit_count",
    "prev_has_py", "prev_has_ts_js", "prev_has_config", "prev_has_md",
    "prev_bash_git", "prev_bash_test_build", "prev_bash_pkg", "prev_bash_file_ops",
    "cum_turn_index", "cum_pushback_count", "cum_correction_count",
    "intent_debug", "intent_refactor", "intent_create", "intent_test",
    "intent_understand", "intent_other",
]


def bootstrap_lr_coefficients(df: pd.DataFrame, target_col: str, n: int = N_BOOTSTRAP) -> dict:
    """Fit LR n times on bootstrap resamples, return per-feature CI."""
    available_cols = [c for c in FEATURE_COLS if c in df.columns]
    X = df[available_cols].fillna(0).values
    y = df[target_col].values.astype(int)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    coef_samples = np.zeros((n, len(available_cols)))
    for i in range(n):
        idx = RNG.choice(len(X_scaled), size=len(X_scaled), replace=True)
        X_boot = X_scaled[idx]
        y_boot = y[idx]
        if y_boot.sum() == 0 or y_boot.sum() == len(y_boot):
            continue
        lr = LogisticRegression(max_iter=1000, solver="lbfgs", random_state=42)
        lr.fit(X_boot, y_boot)
        coef_samples[i] = lr.coef_[0]

    results = {}
    for j, col in enumerate(available_cols):
        vals = coef_samples[:, j]
        results[col] = {
            "mean": round(float(np.mean(vals)), 4),
            "ci_lower": round(float(np.percentile(vals, 2.5)), 4),
            "ci_upper": round(float(np.percentile(vals, 97.5)), 4),
            "std": round(float(np.std(vals)), 4),
        }
    return results


def bootstrap_icc(session_df: pd.DataFrame, n: int = N_BOOTSTRAP) -> dict:
    """Bootstrap ICC on pushback_rate across sessions per user."""
    # Need a user-id column and pushback_rate. Check what's available.
    # Detect user-id column
    if "user_id" in session_df.columns:
        user_col = "user_id"
    elif "repo" in session_df.columns:
        user_col = "repo"
    else:
        return {"icc": None, "note": "no user-id or repo column available for ICC bootstrap"}

    # Filter to users with >=3 sessions
    rate_col = "pushback_rate"
    if rate_col not in session_df.columns:
        # Compute it
        if "n_pushback" in session_df.columns and "n_turns" in session_df.columns:
            session_df = session_df.copy()
            session_df[rate_col] = session_df["n_pushback"] / session_df["n_turns"].clip(lower=1)
        else:
            return {"icc": None, "note": "cannot compute pushback_rate from available columns"}

    multi = session_df.groupby(user_col).filter(lambda g: len(g) >= 3)
    if len(multi) < 30:
        return {"icc": None, "note": f"only {len(multi)} sessions from multi-session users"}

    def compute_icc(data: pd.DataFrame) -> float:
        groups = data.groupby(user_col)[rate_col].apply(list)
        k_values = groups.apply(len)
        n_groups = len(groups)
        if n_groups < 2:
            return 0.0
        grand_mean = data[rate_col].mean()
        # Between-group variance
        ssb = sum(k * (g_mean - grand_mean) ** 2
                  for g_mean, k in zip(groups.apply(np.mean), k_values))
        # Within-group variance
        ssw = sum(sum((x - g_mean) ** 2 for x in g)
                  for g, g_mean in zip(groups, groups.apply(np.mean)))
        msb = ssb / (n_groups - 1) if n_groups > 1 else 0
        msw = ssw / (data.shape[0] - n_groups) if (data.shape[0] - n_groups) > 0 else 0
        k_bar = data.shape[0] / n_groups
        if (msb + (k_bar - 1) * msw) == 0:
            return 0.0
        return float((msb - msw) / (msb + (k_bar - 1) * msw))

    point_icc = compute_icc(multi)
    icc_samples = np.zeros(n)
    users = multi[user_col].unique()
    for i in range(n):
        sampled_users = RNG.choice(users, size=len(users), replace=True)
        boot_df = pd.concat([multi[multi[user_col] == u] for u in sampled_users], ignore_index=True)
        # Reassign user IDs to handle duplicates
        boot_df = boot_df.copy()
        user_map = {u: f"u{j}" for j, u in enumerate(sampled_users)}
        boot_df[user_col] = [f"u{j}" for j, u in enumerate(sampled_users) for _ in range(len(multi[multi[user_col] == u]))]
        # This is approximate; proper bootstrap of ICC is complex
        icc_samples[i] = compute_icc(boot_df) if len(boot_df) > 10 else 0.0

    return {
        "icc_point": round(point_icc, 4),
        "ci_lower": round(float(np.percentile(icc_samples, 2.5)), 4),
        "ci_upper": round(float(np.percentile(icc_samples, 97.5)), 4),
        "n_users": int(len(users)),
        "n_sessions": int(len(multi)),
    }


def main() -> None:
    print("Loading features...")
    df = pd.read_parquet(DATA / "features.parquet")
    session_df = pd.read_parquet(DATA / "session_features.parquet")

    print(f"  Turn-level: {df.shape[0]} rows, {df.shape[1]} cols")
    print(f"  Session-level: {session_df.shape[0]} rows, {session_df.shape[1]} cols")
    print(f"  Session columns: {list(session_df.columns)}")

    # Q1: Bootstrap CIs on pushback prediction coefficients
    print(f"\nQ1: Bootstrap LR coefficients for pushback prediction ({N_BOOTSTRAP} resamples)...")
    q1_cis = bootstrap_lr_coefficients(df, "pushback_is_any")
    print("  Top 5 by |mean|:")
    sorted_q1 = sorted(q1_cis.items(), key=lambda x: abs(x[1]["mean"]), reverse=True)[:5]
    for name, ci in sorted_q1:
        print(f"    {name}: {ci['mean']:.3f} [{ci['ci_lower']:.3f}, {ci['ci_upper']:.3f}]")

    # Q2: Bootstrap CIs on failure-report prediction coefficients
    print(f"\nQ2: Bootstrap LR coefficients for failure_report prediction ({N_BOOTSTRAP} resamples)...")
    q2_cis = bootstrap_lr_coefficients(df, "is_failure_report")
    print("  Top 5 by |mean|:")
    sorted_q2 = sorted(q2_cis.items(), key=lambda x: abs(x[1]["mean"]), reverse=True)[:5]
    for name, ci in sorted_q2:
        print(f"    {name}: {ci['mean']:.3f} [{ci['ci_lower']:.3f}, {ci['ci_upper']:.3f}]")

    # Q4: Bootstrap ICC
    print(f"\nQ4: Bootstrap ICC ({N_BOOTSTRAP} resamples)...")
    icc_result = bootstrap_icc(session_df)
    if icc_result.get("icc_point") is not None:
        print(f"  ICC = {icc_result['icc_point']:.4f} [{icc_result['ci_lower']:.4f}, {icc_result['ci_upper']:.4f}]")
        print(f"  ({icc_result['n_users']} users, {icc_result['n_sessions']} sessions)")
    else:
        print(f"  ICC bootstrap skipped: {icc_result.get('note', 'unknown reason')}")

    output = {
        "q1_pushback_coefficients": q1_cis,
        "q2_failure_report_coefficients": q2_cis,
        "q4_icc": icc_result,
        "n_bootstrap": N_BOOTSTRAP,
        "random_seed": 42,
    }

    out_path = DATA / "bootstrap_cis.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
