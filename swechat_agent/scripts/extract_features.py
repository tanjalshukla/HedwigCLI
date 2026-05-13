"""
Build per-turn and per-session feature matrices from SWE-chat.
Writes data/features.parquet (turn-level) and data/session_features.parquet.

Run once; all analysis scripts read from parquet.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent.parent
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)

WRITE_EDIT_TOOLS = {"Write", "Edit", "NotebookEdit", "write_file", "edit_file"}
READ_TOOLS = {"Read", "Grep", "Glob", "read_file", "grep", "glob", "list_directory", "WebFetch", "WebSearch"}
BASH_TOOLS = {"Bash", "run_command", "shell"}

FILE_EXT_FLAGS = {
    "ts_js": {".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte"},
    "py": {".py"},
    "go": {".go"},
    "rs": {".rs"},
    "md": {".md", ".rst", ".txt"},
    "config": {".json", ".yaml", ".yml", ".toml"},
    "test": set(),  # handled by path heuristic
}


def ext_flag(path: str | None, key: str) -> bool:
    if not path:
        return False
    p = pathlib.PurePath(path)
    if key == "test":
        return "test" in p.name.lower() or "spec" in p.name.lower()
    return p.suffix.lower() in FILE_EXT_FLAGS.get(key, set())


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: install datasets: pip install datasets", file=sys.stderr)
        sys.exit(1)

    print("Loading sessions...")
    sess_ds = load_dataset("SALT-NLP/SWE-chat", "sessions", split="train")
    sessions = sess_ds.to_pandas()
    print(f"  {len(sessions)} sessions")

    print("Loading conversations...")
    conv_ds = load_dataset("SALT-NLP/SWE-chat", "conversations", split="train")
    convs = conv_ds.to_pandas()
    print(f"  {len(convs)} conversation rows")

    return sessions, convs


def build_turn_features(sessions: pd.DataFrame, convs: pd.DataFrame) -> pd.DataFrame:
    print("Building turn-level features...")

    # Index sessions for fast lookup
    sess_idx = sessions.set_index("session_id")

    # Sort convs by session + turn_number for windowed ops
    convs = convs.sort_values(["session_id", "turn_number"]).reset_index(drop=True)

    # Pre-group tool_use rows by session for fast access
    tool_rows = convs[convs["turn_type"] == "tool_use"].copy()
    tool_by_session: dict[str, pd.DataFrame] = {}
    for sid, grp in tool_rows.groupby("session_id"):
        tool_by_session[sid] = grp

    # Pre-group assistant_response rows
    asst_rows = convs[convs["turn_type"] == "assistant_response"].copy()
    asst_by_session: dict[str, pd.DataFrame] = {}
    for sid, grp in asst_rows.groupby("session_id"):
        asst_by_session[sid] = grp

    # Work only on user_prompt rows
    user_prompts = convs[convs["turn_type"] == "user_prompt"].copy()
    print(f"  {len(user_prompts)} user_prompt turns")

    records = []
    # Process per session to compute cumulative state
    for sid, grp in user_prompts.groupby("session_id"):
        grp = grp.sort_values("turn_number").reset_index(drop=True)

        # Session metadata
        if sid in sess_idx.index:
            s = sess_idx.loc[sid]
            sess_persona = s.get("user_persona")
            sess_agent_pct = s.get("agent_percentage")
            sess_duration = s.get("duration_seconds")
            sess_turn_count = s.get("turn_count") or len(grp)
        else:
            sess_persona = None
            sess_agent_pct = None
            sess_duration = None
            sess_turn_count = len(grp)

        tools_s = tool_by_session.get(sid, pd.DataFrame())
        asst_s = asst_by_session.get(sid, pd.DataFrame())

        # Per-session cumulative counters
        cum_pushback = 0
        cum_failure = 0
        cum_correction = 0
        cum_distinct_files: set[str] = set()
        prev_timestamp = None

        n = len(grp)
        for i, row in grp.iterrows():
            tn = row["turn_number"]
            pushback = row.get("prompt_pushback")

            # --- Preceding agent block: tool_use rows with turn_number < tn ---
            prev_tn_cutoff = grp.iloc[grp.index.get_loc(i) - 1]["turn_number"] if i > 0 else -1

            if not tools_s.empty:
                prev_tools = tools_s[
                    (tools_s["turn_number"] > prev_tn_cutoff) &
                    (tools_s["turn_number"] < tn)
                ]
            else:
                prev_tools = pd.DataFrame()

            prev_tool_count = len(prev_tools)
            prev_bash_count = int(prev_tools["tool_name"].isin(BASH_TOOLS).sum()) if not prev_tools.empty else 0
            prev_write_count = int(prev_tools["tool_name"].isin(WRITE_EDIT_TOOLS).sum()) if not prev_tools.empty else 0
            prev_read_count = int(prev_tools["tool_name"].isin(READ_TOOLS).sum()) if not prev_tools.empty else 0

            # File types in preceding tools
            touched_paths = prev_tools["file_path"].dropna().tolist() if not prev_tools.empty else []
            for fp in touched_paths:
                if fp:
                    cum_distinct_files.add(fp)

            prev_has_ts = {k: any(ext_flag(p, k) for p in touched_paths) for k in FILE_EXT_FLAGS}
            prev_has_test = any(ext_flag(p, "test") for p in touched_paths)

            # Bash category for preceding bash calls
            if not prev_tools.empty:
                bash_cats = prev_tools[prev_tools["tool_name"].isin(BASH_TOOLS)]["bash_category"].dropna()
                prev_bash_cat = bash_cats.mode()[0] if len(bash_cats) else None
            else:
                prev_bash_cat = None

            # Preceding assistant response word count
            if not asst_s.empty:
                prev_asst = asst_s[
                    (asst_s["turn_number"] > prev_tn_cutoff) &
                    (asst_s["turn_number"] < tn)
                ]
                prev_resp_words = int(prev_asst["word_count"].sum()) if not prev_asst.empty else 0
            else:
                prev_resp_words = 0

            # Time since previous user_prompt
            cur_ts = row.get("timestamp")
            if cur_ts is not None and prev_timestamp is not None:
                try:
                    delta_s = (pd.Timestamp(cur_ts) - pd.Timestamp(prev_timestamp)).total_seconds()
                    time_since_prev = max(0.0, delta_s)
                except Exception:
                    time_since_prev = None
            else:
                time_since_prev = None

            # Session position
            pos_frac = i / max(1, n - 1) if n > 1 else 0.0
            session_third = min(2, int(pos_frac * 3))

            rec = {
                # Keys
                "turn_id": row.get("turn_id"),
                "session_id": sid,
                "turn_number": tn,
                # Turn basics
                "turn_index": i,
                "session_third": session_third,
                "session_position_frac": round(pos_frac, 4),
                "is_first_turn": bool(row.get("is_first_turn")),
                "is_continuation": bool(row.get("is_continuation")),
                "prompt_word_count": row.get("word_count") or 0,
                "prompt_char_count": row.get("char_count") or 0,
                "prompt_intent": row.get("prompt_intent"),
                "language": row.get("language"),
                # Target
                "prompt_pushback": pushback,
                "pushback_is_any": int(pushback in {"correction", "rejection", "failure_report"}) if pushback else 0,
                "is_failure_report": int(pushback == "failure_report") if pushback else 0,
                # Preceding agent block
                "prev_tool_count": prev_tool_count,
                "prev_bash_count": prev_bash_count,
                "prev_write_edit_count": prev_write_count,
                "prev_read_count": prev_read_count,
                "prev_resp_word_count": prev_resp_words,
                "prev_bash_category": prev_bash_cat,
                "prev_has_ts_js": int(prev_has_ts.get("ts_js", False)),
                "prev_has_py": int(prev_has_ts.get("py", False)),
                "prev_has_go": int(prev_has_ts.get("go", False)),
                "prev_has_config": int(prev_has_ts.get("config", False)),
                "prev_has_md": int(prev_has_ts.get("md", False)),
                "prev_has_test": int(prev_has_test),
                # Time
                "time_since_prev_s": time_since_prev,
                # Cumulative state at this turn
                "cum_turn_index": i,
                "cum_pushback_count": cum_pushback,
                "cum_failure_count": cum_failure,
                "cum_correction_count": cum_correction,
                "cum_distinct_files": len(cum_distinct_files),
                # Session metadata
                "sess_persona": sess_persona,
                "sess_agent_pct": sess_agent_pct,
                "sess_duration_s": sess_duration,
                "sess_total_turns": sess_turn_count,
            }
            records.append(rec)

            # Update cumulative state after this turn
            if pushback in {"correction", "rejection", "failure_report"}:
                cum_pushback += 1
            if pushback == "failure_report":
                cum_failure += 1
            if pushback == "correction":
                cum_correction += 1
            if cur_ts is not None:
                prev_timestamp = cur_ts

    df = pd.DataFrame(records)
    print(f"  Built {len(df)} turn rows")
    return df


def build_session_features(turns: pd.DataFrame, sessions: pd.DataFrame) -> pd.DataFrame:
    print("Building session-level features...")

    grp = turns.groupby("session_id")

    agg = grp.agg(
        n_turns=("turn_index", "count"),
        pushback_count=("pushback_is_any", "sum"),
        failure_count=("is_failure_report", "sum"),
        correction_count=("cum_correction_count", "max"),
        mean_prompt_words=("prompt_word_count", "mean"),
        mean_prev_tools=("prev_tool_count", "mean"),
        mean_prev_bash=("prev_bash_count", "mean"),
        mean_prev_write=("prev_write_edit_count", "mean"),
        mean_time_between_s=("time_since_prev_s", "mean"),
        distinct_tasks=("prompt_word_count", lambda x: x.nunique()),  # approximate
    ).reset_index()

    agg["pushback_rate"] = agg["pushback_count"] / agg["n_turns"].clip(lower=1)
    agg["failure_rate"] = agg["failure_count"] / agg["n_turns"].clip(lower=1)
    agg["correction_rate"] = agg["correction_count"] / agg["n_turns"].clip(lower=1)

    # Merge in sessions fields
    sess_cols = ["session_id", "user_persona", "agent_percentage", "duration_seconds",
                 "turn_count", "files_touched_count", "action_count", "research_count",
                 "tool_call_count", "user_id"]
    sess_sub = sessions[sess_cols].copy()

    merged = agg.merge(sess_sub, on="session_id", how="left")
    # Distinct tasks from the turn prompt texts - use content-based uniqueness
    # (already approximated by nunique of word_count as a proxy — replace with actual below)
    distinct_real = turns.groupby("session_id")["prompt_word_count"].apply(
        lambda x: x.nunique()
    ).reset_index(name="distinct_task_wc_proxy")
    merged = merged.merge(distinct_real, on="session_id", how="left")

    print(f"  {len(merged)} session rows")
    return merged


def main() -> None:
    sessions, convs = load_data()
    turns = build_turn_features(sessions, convs)
    turns.to_parquet(DATA / "features.parquet", index=False)
    print(f"Wrote data/features.parquet ({len(turns)} rows)")

    sess_feats = build_session_features(turns, sessions)
    sess_feats.to_parquet(DATA / "session_features.parquet", index=False)
    print(f"Wrote data/session_features.parquet ({len(sess_feats)} rows)")


if __name__ == "__main__":
    main()
