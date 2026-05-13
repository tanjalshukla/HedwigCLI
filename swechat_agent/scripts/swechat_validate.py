"""
Validate Hedwig's inference against SWE-chat ground-truth labels.

Reads per-session JSONL from data/swechat/sessions/, runs Hedwig's
preference inference, and compares results to the _swechat_* shadow fields.

Outputs data/swechat/validation.json with per-category agreement rates.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict

# Allow importing from sc/ sibling
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from sc.preference_inference import (
    classify_pushback,
    infer_coding_mode,
    infer_user_persona,
    summarize_session,
)
from sc.preferences import CodingMode, PushbackType, UserPersona

HERE = pathlib.Path(__file__).resolve().parent.parent  # swechat_agent/
SESSIONS_DIR = HERE / "data" / "swechat" / "sessions"
VALIDATION_PATH = HERE / "data" / "swechat" / "validation.json"

# Normalise SWE-chat persona strings to Hedwig enum values
_PERSONA_TO_HEDWIG = {
    "expert_nitpicker": UserPersona.EXPERT_NITPICKER,
    "vague_requester": UserPersona.VAGUE_REQUESTER,
    "mind_changer": UserPersona.MIND_CHANGER,
    "other": None,  # no direct equivalent; excluded from comparison
}

_PUSHBACK_TO_HEDWIG = {
    "correction": PushbackType.CORRECTION,
    "rejection": PushbackType.REJECTION,
    "failure_report": PushbackType.FAILURE_REPORT,
    "non_pushback": PushbackType.NON_PUSHBACK,
    # These SWE-chat categories don't map cleanly; we exclude them.
    "pacing_complaint": None,
    "takeover": None,
    "requirement_change": None,
}

_MODE_TO_HEDWIG = {
    "vibe": CodingMode.VIBE,
    "collaborative": CodingMode.COLLABORATIVE,
    "human_only": CodingMode.HUMAN_ONLY,
}


def _confusion_matrix(
    labels: list[tuple[str, str]],
) -> dict[str, dict[str, int]]:
    """Build a {ground_truth: {predicted: count}} matrix."""
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for gt, pred in labels:
        matrix[gt][pred] += 1
    return {k: dict(v) for k, v in matrix.items()}


def _agreement_rate(labels: list[tuple[str, str]]) -> float:
    if not labels:
        return 0.0
    correct = sum(1 for gt, pred in labels if gt == pred)
    return correct / len(labels)


def validate() -> None:
    jsonl_files = sorted(SESSIONS_DIR.glob("*.jsonl"))
    if not jsonl_files:
        print(f"No session files found in {SESSIONS_DIR}. Run swechat_extract.py first.")
        sys.exit(1)

    print(f"Validating {len(jsonl_files)} sessions...")

    mode_labels: list[tuple[str, str]] = []
    persona_labels: list[tuple[str, str]] = []
    pushback_labels: list[tuple[str, str]] = []

    skipped_no_gt = 0
    n_sessions_processed = 0

    for fpath in jsonl_files:
        rows = []
        with open(fpath) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))

        if not rows:
            continue

        n_sessions_processed += 1

        # --- Session-level inference ---
        summary = summarize_session(rows)

        # Coding mode
        gt_mode_str = rows[0].get("_swechat_mode")
        if gt_mode_str:
            hedwig_mode = infer_coding_mode(summary)
            gt_mode = _MODE_TO_HEDWIG.get(gt_mode_str)
            if gt_mode is not None:
                mode_labels.append((gt_mode.value, hedwig_mode.value))

        # User persona
        gt_persona_str = rows[0].get("_swechat_persona")
        if gt_persona_str:
            hedwig_persona = infer_user_persona(summary)
            gt_persona = _PERSONA_TO_HEDWIG.get(gt_persona_str)
            if gt_persona is not None and summary.n_turns >= 3:
                persona_labels.append((gt_persona.value, hedwig_persona.value))

        # --- Per-turn pushback classification ---
        for row in rows:
            gt_pushback_str = row.get("_swechat_pushback")
            if not gt_pushback_str:
                continue
            gt_pushback = _PUSHBACK_TO_HEDWIG.get(gt_pushback_str)
            if gt_pushback is None:
                # SWE-chat category with no clean Hedwig equivalent; skip
                continue

            hedwig_pushback = classify_pushback(
                user_decision=row.get("user_decision"),
                edit_distance=row.get("edit_distance"),
                user_feedback_text=row.get("user_feedback_text"),
            )
            pushback_labels.append((gt_pushback.value, hedwig_pushback.value))

    print(f"  {n_sessions_processed} sessions processed")
    print(f"  {len(mode_labels)} coding mode comparisons")
    print(f"  {len(persona_labels)} persona comparisons")
    print(f"  {len(pushback_labels)} pushback comparisons")

    mode_agreement = _agreement_rate(mode_labels)
    persona_agreement = _agreement_rate(persona_labels)
    pushback_agreement = _agreement_rate(pushback_labels)

    print(f"\nAgreement rates:")
    print(f"  coding_mode:  {mode_agreement:.3f} ({len(mode_labels)} samples)")
    print(f"  user_persona: {persona_agreement:.3f} ({len(persona_labels)} samples)")
    print(f"  pushback:     {pushback_agreement:.3f} ({len(pushback_labels)} samples)")

    validation = {
        "n_sessions": n_sessions_processed,
        "coding_mode": {
            "agreement_rate": round(mode_agreement, 4),
            "n_comparisons": len(mode_labels),
            "confusion_matrix": _confusion_matrix(mode_labels),
        },
        "user_persona": {
            "agreement_rate": round(persona_agreement, 4),
            "n_comparisons": len(persona_labels),
            "confusion_matrix": _confusion_matrix(persona_labels),
            "note": "Only sessions with >=3 turns and non-null, non-'other' persona included",
        },
        "pushback": {
            "agreement_rate": round(pushback_agreement, 4),
            "n_comparisons": len(pushback_labels),
            "confusion_matrix": _confusion_matrix(pushback_labels),
            "note": (
                "pacing_complaint / takeover / requirement_change excluded "
                "(no clean Hedwig equivalent)"
            ),
        },
        "proxy_caveats": [
            "edit_distance is session-level proxy (1 - agent_pct), not per-turn",
            "stage is hardcoded to 'apply' for all turns",
            "user_decision is derived from pushback label, creating circularity "
            "in pushback classification test — see findings note",
        ],
    }

    with open(VALIDATION_PATH, "w") as f:
        json.dump(validation, f, indent=2)
    print(f"\nValidation report written to {VALIDATION_PATH}")


if __name__ == "__main__":
    validate()
