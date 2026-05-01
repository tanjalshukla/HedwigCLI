#!/usr/bin/env python3
"""Build the Hedwig vs. Claude Code comparison table.

Reads manually recorded outcomes from results.csv and prints a formatted
comparison table plus writes comparison_table.csv.

Usage:
    python scripts/claude_code_eval/build_comparison_table.py

Outputs:
    - Terminal table
    - scripts/claude_code_eval/comparison_table.csv
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

RESULTS_CSV = Path(__file__).parent / "results.csv"
OUTPUT_CSV = Path(__file__).parent / "comparison_table.csv"

TASK_LABELS = {
    "task1_summary_endpoint": "Summary endpoint",
    "task2_priority_filter": "Priority filter",
}


def _ratio(checkin: str, total: str) -> str:
    try:
        return f"{int(checkin) / int(total):.2f}"
    except (ValueError, ZeroDivisionError):
        return "?"


def main() -> None:
    if not RESULTS_CSV.exists():
        print(f"ERROR: {RESULTS_CSV} not found.")
        sys.exit(1)

    with RESULTS_CSV.open() as f:
        rows = list(csv.DictReader(f))

    results = []
    for row in rows:
        checkin = row["checkin_count"].strip()
        auto = row["auto_count"].strip()
        total = row["total_operations"].strip()
        results.append({
            "run_id": row["run_id"],
            "system": row["system"],
            "persona": row["persona"],
            "task": TASK_LABELS.get(row["task"], row["task"]),
            "checkins": checkin if checkin else "?",
            "autos": auto if auto else "?",
            "total": total if total else "?",
            "ratio": _ratio(checkin, total),
            "notes": row["notes"].strip(),
        })

    # Per-task table
    print("\n" + "=" * 100)
    print("Hedwig vs. Claude Code: Approvals per Operation by Persona and Task")
    print("=" * 100)
    print(f"{'Run':<6}  {'System':<12}  {'Persona':<12}  {'Task':<22}  {'Check-ins':>10}  {'Auto':>6}  {'Total':>7}  {'Ratio':>7}")
    print("-" * 100)
    for r in results:
        print(f"{r['run_id']:<6}  {r['system']:<12}  {r['persona']:<12}  {r['task']:<22}  {r['checkins']:>10}  {r['autos']:>6}  {r['total']:>7}  {r['ratio']:>7}")
    print("=" * 100)

    # Per-run totals
    print("\nTotals by run (approvals / total operations):")
    from itertools import groupby
    for run_id, group in groupby(results, key=lambda x: x["run_id"]):
        rows_group = list(group)
        try:
            total_checkins = sum(int(r["checkins"]) for r in rows_group)
            total_ops = sum(int(r["total"]) for r in rows_group)
            ratio = f"{total_checkins / total_ops:.2f}" if total_ops else "?"
            system = rows_group[0]["system"]
            persona = rows_group[0]["persona"]
            print(f"  {run_id:<6}  {system:<12}  {persona:<12}  check-ins={total_checkins}  total_ops={total_ops}  ratio={ratio}")
        except ValueError:
            print(f"  {run_id:<6}  incomplete data")

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["run_id", "system", "persona", "task", "checkins", "autos", "total", "ratio", "notes"])
        writer.writeheader()
        writer.writerows(results)
    print(f"\nCSV written to: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
