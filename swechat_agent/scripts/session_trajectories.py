"""
Q4: Does behavior change within a session? And across sessions for same user?
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pandas as pd
from scipy import stats

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"


def within_session_trends(df: pd.DataFrame) -> dict:
    print("\n=== Q4a: Within-session behavior change ===")

    # Bucket each turn into early(0)/mid(1)/late(2)
    metrics = ["pushback_is_any", "is_failure_report", "prompt_word_count",
               "prev_tool_count", "prev_bash_count", "prev_write_edit_count"]

    # Only use sessions with >= 6 turns for meaningful thirds
    sess_sizes = df.groupby("session_id")["turn_index"].max()
    long_sessions = sess_sizes[sess_sizes >= 5].index
    sub = df[df["session_id"].isin(long_sessions)].copy()
    print(f"  Sessions with >=6 turns: {len(long_sessions)} ({len(sub)} turns)")

    results = {}
    print(f"\n  {'Metric':<35} {'Early':>8} {'Mid':>8} {'Late':>8}  {'trend'}")
    print(f"  {'-'*35} {'-'*8} {'-'*8} {'-'*8}  {'------'}")
    for m in metrics:
        if m not in sub.columns:
            continue
        grp = sub.groupby("session_third")[m].agg(["mean", "sem"])
        e = grp.loc[0, "mean"] if 0 in grp.index else np.nan
        mid = grp.loc[1, "mean"] if 1 in grp.index else np.nan
        late = grp.loc[2, "mean"] if 2 in grp.index else np.nan

        # Test early vs late with Mann-Whitney
        early_vals = sub[sub["session_third"] == 0][m].dropna()
        late_vals = sub[sub["session_third"] == 2][m].dropna()
        if len(early_vals) > 10 and len(late_vals) > 10:
            stat, p = stats.mannwhitneyu(early_vals, late_vals, alternative="two-sided")
            sig = "**" if p < 0.001 else ("*" if p < 0.05 else "ns")
        else:
            p, sig = np.nan, "?"

        trend = "↓" if (not np.isnan(e) and not np.isnan(late) and late < e) else "↑" if (not np.isnan(e) and not np.isnan(late) and late > e) else "~"
        print(f"  {m:<35} {e:>8.3f} {mid:>8.3f} {late:>8.3f}  {trend} p={p:.4f} {sig}")
        results[m] = {
            "early": round(float(e), 4) if not np.isnan(e) else None,
            "mid": round(float(mid), 4) if not np.isnan(mid) else None,
            "late": round(float(late), 4) if not np.isnan(late) else None,
            "p_early_vs_late": round(float(p), 6) if not np.isnan(p) else None,
            "significant": sig,
        }

    return results


def cross_session_stability(df: pd.DataFrame, sessions: pd.DataFrame) -> dict:
    print("\n=== Q4b: Cross-session stability per user ===")

    # Build session-level pushback_rate
    sess_feats = df.groupby("session_id").agg(
        pushback_rate=("pushback_is_any", "mean"),
        n_turns=("turn_index", "count"),
    ).reset_index()

    # Join user_id
    sess_feats = sess_feats.merge(
        sessions[["session_id", "user_id"]].dropna(subset=["user_id"]),
        on="session_id", how="inner"
    )

    users_multi = sess_feats.groupby("user_id").filter(lambda x: len(x) >= 3)
    n_users = users_multi["user_id"].nunique()
    print(f"  Users with >=3 sessions: {n_users}")

    if n_users == 0:
        print("  Not enough multi-session users for stability analysis")
        return {"n_users_with_3plus_sessions": 0}

    # ICC-like: ratio of between-user variance to total variance
    # Simple: compute per-user mean and std of pushback_rate
    user_stats = users_multi.groupby("user_id")["pushback_rate"].agg(["mean", "std", "count"])
    within_var = user_stats["std"].pow(2).mean()
    between_var = user_stats["mean"].var()
    total_var = users_multi["pushback_rate"].var()

    icc = between_var / (between_var + within_var) if (between_var + within_var) > 0 else 0.0
    print(f"  Between-user variance: {between_var:.4f}")
    print(f"  Within-user variance:  {within_var:.4f}")
    print(f"  ICC (stability): {icc:.3f}")
    print(f"  Interpretation: {'high (>0.6)' if icc > 0.6 else 'moderate (0.4-0.6)' if icc > 0.4 else 'low (<0.4)'}")

    # Show a few users
    top_users = user_stats.sort_values("count", ascending=False).head(5)
    print("\n  Sample user pushback rates across sessions:")
    for uid, row in top_users.iterrows():
        sessions_for_user = users_multi[users_multi["user_id"] == uid]["pushback_rate"].values
        print(f"    {str(uid)[:20]:20s}: n={int(row['count'])} sessions, "
              f"pushback_rates={[round(float(x),2) for x in sessions_for_user[:5]]}")

    return {
        "n_users_with_3plus_sessions": n_users,
        "icc": round(float(icc), 4),
        "between_user_var": round(float(between_var), 4),
        "within_user_var": round(float(within_var), 4),
        "interpretation": "high" if icc > 0.6 else "moderate" if icc > 0.4 else "low",
    }


def main() -> None:
    df = pd.read_parquet(DATA / "features.parquet")
    try:
        sessions = pd.read_parquet(DATA / "session_features.parquet")
    except FileNotFoundError:
        sessions = pd.DataFrame(columns=["session_id", "user_id"])

    within = within_session_trends(df)
    cross = cross_session_stability(df, sessions)

    results = {"within_session": within, "cross_session": cross}
    out = DATA / "trajectories.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWritten to {out}")


if __name__ == "__main__":
    main()
