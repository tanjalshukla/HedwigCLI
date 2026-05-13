"""
V4: Failure-signal trigger precision measurement.

Trigger definition (from BRIEF_V4.md):
  intent_debug == 1  →  prompt_intent == "debug"
  prev_bash_count >= 2
  cum_failure_count >= 1  (at least one prior failure in the session)

For turns where this fires, what fraction of the NEXT turn is a failure report?
"""
from __future__ import annotations

import json
import pathlib

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"

ACTIVE_TURNS_MIN = 12  # from v3 cluster analysis


def main() -> None:
    print("Loading features...")
    df = pd.read_parquet(DATA / "features.parquet")
    session_df = pd.read_parquet(DATA / "session_features.parquet")
    print(f"  {df.shape[0]} turns, {df.shape[1]} cols")

    # Sort so shift(-1) gives next turn within session
    df = df.sort_values(["session_id", "turn_index"]).reset_index(drop=True)

    # Step 1: build trigger mask using actual column names
    trigger_mask = (
        (df["prompt_intent"] == "debug") &
        (df["prev_bash_count"] >= 2) &
        (df["cum_failure_count"] >= 1)
    )
    T = df[trigger_mask].copy()
    print(f"\nTrigger matches (T): {len(T)} of {len(df)} turns ({100*len(T)/len(df):.2f}%)")

    # Step 2: get next-turn signals within each session
    df["next_is_failure"] = df.groupby("session_id")["is_failure_report"].shift(-1)
    df["next_pushback_any"] = df.groupby("session_id")["pushback_is_any"].shift(-1)
    df["next_pushback_type"] = df.groupby("session_id")["prompt_pushback"].shift(-1)

    # Re-apply trigger mask on df with next-turn columns, drop last turn per session
    T_next = df[
        (df["prompt_intent"] == "debug") &
        (df["prev_bash_count"] >= 2) &
        (df["cum_failure_count"] >= 1) &
        (df["next_is_failure"].notna())
    ].copy()
    print(f"Trigger matches with successor (T_next): {len(T_next)}")

    # Step 3: Precision
    next_failures = int(T_next["next_is_failure"].sum())
    n_T_next = len(T_next)
    precision = next_failures / n_T_next if n_T_next > 0 else 0.0
    baseline_failure_rate = df["is_failure_report"].mean()
    lift = precision / baseline_failure_rate if baseline_failure_rate > 0 else 0.0

    print(f"\n=== HEADLINE ===")
    print(f"|T|              = {len(T)}")
    print(f"|T_next|         = {n_T_next}")
    print(f"Next-turn failures = {next_failures}")
    print(f"PRECISION        = {precision:.4f} ({precision*100:.1f}%)")
    print(f"Baseline rate    = {baseline_failure_rate:.4f} ({baseline_failure_rate*100:.1f}%)")
    print(f"LIFT             = {lift:.2f}x")

    # Any-pushback bonus metric
    next_any = int(T_next["next_pushback_any"].sum())
    any_precision = next_any / n_T_next if n_T_next > 0 else 0.0
    baseline_any = df["pushback_is_any"].mean()
    any_lift = any_precision / baseline_any if baseline_any > 0 else 0.0
    print(f"\nAny-pushback precision = {any_precision:.4f} ({any_precision*100:.1f}%), lift={any_lift:.2f}x")

    # Step 4: Stratify by session intensity
    intensity_map = session_df.set_index("session_id")["n_turns"].apply(
        lambda x: "active" if x >= ACTIVE_TURNS_MIN else "delegating"
    ).to_dict()
    T_next["intensity"] = T_next["session_id"].map(intensity_map).fillna("unknown")

    print(f"\n=== STRATIFIED BY INTENSITY ===")
    stratified = {}
    for intensity in ["active", "delegating"]:
        sub = T_next[T_next["intensity"] == intensity]
        if len(sub) == 0:
            print(f"  {intensity}: no matches")
            continue
        sub_prec = float(sub["next_is_failure"].sum() / len(sub))
        sub_lift = sub_prec / baseline_failure_rate if baseline_failure_rate > 0 else 0.0
        print(f"  {intensity}: n={len(sub)}, precision={sub_prec:.4f} ({sub_prec*100:.1f}%), lift={sub_lift:.2f}x")
        stratified[intensity] = {
            "n": int(len(sub)),
            "next_failures": int(sub["next_is_failure"].sum()),
            "precision": round(sub_prec, 4),
            "lift": round(sub_lift, 2),
        }

    # Step 5: False-positive breakdown
    fp = T_next[T_next["next_is_failure"] == 0]
    breakdown = fp["next_pushback_type"].value_counts().to_dict()
    print(f"\n=== FALSE-POSITIVE BREAKDOWN ===")
    print(f"Next turn was NOT a failure: {len(fp)}")
    for ptype, cnt in sorted(breakdown.items(), key=lambda x: -x[1]):
        print(f"  {ptype}: {cnt} ({100*cnt/len(fp):.1f}%)")

    # Variant: tighter trigger (bash >= 3 or failure count >= 2)
    T3 = df[
        (df["prompt_intent"] == "debug") &
        (df["prev_bash_count"] >= 3) &
        (df["cum_failure_count"] >= 1) &
        (df["next_is_failure"].notna())
    ]
    prec3 = float(T3["next_is_failure"].sum() / len(T3)) if len(T3) > 0 else 0.0

    T2f = df[
        (df["prompt_intent"] == "debug") &
        (df["prev_bash_count"] >= 2) &
        (df["cum_failure_count"] >= 2) &
        (df["next_is_failure"].notna())
    ]
    prec2f = float(T2f["next_is_failure"].sum() / len(T2f)) if len(T2f) > 0 else 0.0

    print(f"\n=== TIGHTER VARIANTS ===")
    print(f"  bash>=3, fail>=1: n={len(T3)}, precision={prec3:.4f} ({prec3*100:.1f}%), lift={prec3/baseline_failure_rate:.2f}x")
    print(f"  bash>=2, fail>=2: n={len(T2f)}, precision={prec2f:.4f} ({prec2f*100:.1f}%), lift={prec2f/baseline_failure_rate:.2f}x")

    # Recommendation
    if lift >= 2.0:
        recommendation = "ship_as_is"
        reason = f"Precision {precision*100:.1f}% is {lift:.1f}x baseline — strong enough to ship."
    elif lift >= 1.5:
        recommendation = "consider_tightening"
        reason = f"Precision {precision*100:.1f}% is {lift:.1f}x baseline — acceptable but tighter variant worth checking."
    else:
        recommendation = "tighten_trigger"
        reason = f"Precision {precision*100:.1f}% is only {lift:.1f}x baseline — trigger fires too broadly."
    print(f"\n=== RECOMMENDATION ===")
    print(f"  {recommendation}: {reason}")

    output = {
        "trigger_definition": {
            "conditions": [
                "prompt_intent == 'debug'",
                "prev_bash_count >= 2",
                "cum_failure_count >= 1",
            ]
        },
        "headline": {
            "trigger_matches": int(len(T)),
            "trigger_matches_with_successor": n_T_next,
            "next_turn_failures": next_failures,
            "precision": round(precision, 4),
            "baseline_failure_rate": round(float(baseline_failure_rate), 4),
            "lift": round(lift, 2),
        },
        "any_pushback": {
            "next_turn_any_pushback": next_any,
            "precision": round(any_precision, 4),
            "baseline_pushback_rate": round(float(baseline_any), 4),
            "lift": round(any_lift, 2),
        },
        "stratified": stratified,
        "false_positive_breakdown": {str(k): int(v) for k, v in breakdown.items()},
        "tighter_variants": {
            "bash_ge3_fail_ge1": {
                "n": int(len(T3)),
                "precision": round(prec3, 4),
                "lift": round(prec3 / baseline_failure_rate, 2) if baseline_failure_rate > 0 else 0,
            },
            "bash_ge2_fail_ge2": {
                "n": int(len(T2f)),
                "precision": round(prec2f, 4),
                "lift": round(prec2f / baseline_failure_rate, 2) if baseline_failure_rate > 0 else 0,
            },
        },
        "recommendation": recommendation,
        "recommendation_reason": reason,
    }

    out_path = DATA / "trigger_precision.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
