from __future__ import annotations

"""Low-level helpers shared across run orchestration modules."""

import difflib
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from rich import print

from ..agent_client import ClaudeClient
from ..autonomy import preferences_from_model_payload
from ..cli_shared import (
    read_file_context as _read_file_context,
    resolve_config as _resolve_config,
    truncate_content as _truncate_content,
)
from ..policy import PolicyDecision, PolicyInput, decide_action
from ..schema import IntentDeclaration
from ..session import ClaudeSession
from ..trust_db import HardConstraint, PolicyHistory, TrustDB


@dataclass(frozen=True)
class StudyContext:
    participant_id: str | None = None
    study_run_id: str | None = None
    study_task_id: str | None = None
    autonomy_mode: str | None = None


@dataclass(frozen=True)
class SpecContext:
    path: str
    digest: str
    sha256: str


def _append_file_context(
    session: ClaudeSession,
    files: list[str],
    repo_root: Path,
    max_chars: int,
) -> None:
    blocks: list[str] = []
    for path in files:
        file_path = repo_root / path
        try:
            content = file_path.read_text()
        except Exception:
            content = ""
        content = _truncate_content(content, max_chars)
        blocks.append(f"FILE: {path}\n-----\n{content}\n-----")
    if blocks:
        session.add_user("Requested file contents:\n" + "\n\n".join(blocks))


def _line_delta_size(old_text: str, new_text: str) -> int:
    matcher = difflib.SequenceMatcher(a=old_text.splitlines(), b=new_text.splitlines())
    delta = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            delta += i2 - i1
        if tag in {"replace", "insert"}:
            delta += j2 - j1
    return delta


def _plan_hash(declaration: IntentDeclaration) -> str:
    payload = json.dumps(
        declaration.model_dump(exclude_none=False, mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collect_change_metrics(repo_root: Path, updates: dict[str, str]) -> dict[str, tuple[int, bool]]:
    metrics: dict[str, tuple[int, bool]] = {}
    for path, new_content in updates.items():
        file_path = repo_root / path
        old_content = ""
        if file_path.exists():
            try:
                old_content = file_path.read_text()
            except Exception:
                old_content = ""
        delta_size = _line_delta_size(_normalize_line_endings(old_content), _normalize_line_endings(new_content))
        metrics[path] = (delta_size, not file_path.exists())
    return metrics


def _policy_decision_for_file(
    *,
    history: PolicyHistory,
    diff_size: int,
    blast_radius: int,
    is_new_file: bool,
    is_security_sensitive: bool,
    change_pattern: str | None,
    recent_denials: int,
    files_in_action: int,
    verification_failure_rate: float | None = None,
    model_confidence_avg: float | None = None,
    model_confidence_samples: int = 0,
    proceed_threshold: float,
    flag_threshold: float,
) -> PolicyDecision:
    return decide_action(
        PolicyInput(
            prior_approvals=history.effective_approvals,
            prior_denials=history.denials,
            avg_response_ms=history.avg_response_ms,
            avg_edit_distance=history.avg_edit_distance or 0.0,
            diff_size=diff_size,
            blast_radius=blast_radius,
            is_new_file=is_new_file,
            is_security_sensitive=is_security_sensitive,
            change_pattern=change_pattern,
            recent_denials=recent_denials,
            files_in_action=files_in_action,
            verification_failure_rate=verification_failure_rate,
            model_confidence_avg=model_confidence_avg,
            model_confidence_samples=model_confidence_samples,
        ),
        proceed_threshold=proceed_threshold,
        flag_threshold=flag_threshold,
    )


def _build_patch_from_updates(
    repo_root: Path,
    updates: dict[str, str],
) -> tuple[str, list[str]]:
    diffs: list[str] = []
    touched: list[str] = []
    for path, new_content in updates.items():
        file_path = repo_root / path
        old_content = ""
        if file_path.exists():
            try:
                old_content = file_path.read_text()
            except Exception:
                old_content = ""
        old_norm = _normalize_line_endings(old_content)
        new_norm = _normalize_line_endings(new_content)
        old_has_trailing_newline = old_content.endswith("\n") or old_content.endswith("\r\n")
        if old_has_trailing_newline and new_norm and not new_norm.endswith("\n"):
            new_norm += "\n"
        if new_norm == old_norm:
            continue
        fromfile = "/dev/null" if not file_path.exists() else f"a/{path}"
        tofile = f"b/{path}"
        old_lines = old_norm.splitlines(keepends=True)
        new_lines = new_norm.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=fromfile,
                tofile=tofile,
                lineterm="",
            )
        )
        if diff_lines:
            diffs.append("\n".join(diff_lines))
            touched.append(path)
    patch_text = "\n".join(diffs).strip()
    if patch_text and not patch_text.endswith("\n"):
        patch_text += "\n"
    return patch_text, touched


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_new_content(old_content: str, new_content: str) -> str:
    old_has_trailing_newline = old_content.endswith("\n") or old_content.endswith("\r\n")
    new_norm = _normalize_line_endings(new_content)
    if old_has_trailing_newline and new_norm and not new_norm.endswith("\n"):
        new_norm += "\n"
    if "\r\n" in old_content:
        return new_norm.replace("\n", "\r\n")
    return new_norm


def _constraint_index(
    trust_db: TrustDB,
    repo_root: str,
    files: list[str],
    *,
    access_type: str = "write",
) -> dict[str, HardConstraint | None]:
    return {
        path: trust_db.strongest_constraint(repo_root, path, access_type=access_type)
        for path in files
    }


def _auto_read_user_decision(
    path: str,
    read_leases: dict[str, str | None],
    read_policies: dict[str, PolicyDecision],
) -> str:
    if read_leases[path] is not None:
        return "auto_approve_read_lease"
    return "auto_approve_flag" if read_policies[path].action == "proceed_flag" else "auto_approve"


def _learn_preferences_from_feedback(
    *,
    trust_db: TrustDB,
    repo_root: str,
    feedback_text: str,
    client: ClaudeClient | None = None,
) -> list[str]:
    """Learn autonomy preferences from feedback using model classification."""
    if not client:
        return []
    model_payload = client.summarize_autonomy_feedback(feedback_text)
    if not model_payload:
        return []
    inferred = preferences_from_model_payload(model_payload)
    return trust_db.merge_autonomy_preferences(repo_root, inferred)


def _apply_feedback_learning(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session: ClaudeSession,
    feedback_text: str | None,
    client: ClaudeClient | None = None,
    guidance_prefix: str | None = None,
) -> list[str]:
    """Learn preferences from free-text feedback and persist memory notes."""
    if not feedback_text:
        return []
    learned = _learn_preferences_from_feedback(
        trust_db=trust_db,
        repo_root=repo_root,
        feedback_text=feedback_text,
        client=client,
    )
    for item in learned:
        print(f"[green]Learned preference:[/green] {item}")
        session.add_memory_note(f"Learned autonomy preference: {item}")
    if guidance_prefix:
        session.add_memory_note(f"{guidance_prefix}: {feedback_text}")
    return learned


def _capture_logic_notes(
    *,
    trust_db: TrustDB,
    repo_root: str,
    session_id: str,
    task: str,
    declaration: IntentDeclaration,
    touched_files: list[str],
    patch_text: str,
    spec_context: SpecContext | None,
    client: ClaudeClient | None,
) -> list[str]:
    """Summarize completed work into reusable functionality notes."""
    if not client or not touched_files or not patch_text.strip():
        return []

    feedback_texts = [
        str(row["user_feedback_text"]).strip()
        for row in trust_db.session_traces(repo_root, session_id)
        if row["user_feedback_text"]
    ]
    verification_passed = trust_db.session_verification_status(repo_root, session_id)
    patch_excerpt = patch_text[:2200]
    try:
        result = client.summarize_logic_notes(
            task=task,
            intent_summary=declaration.task_summary,
            touched_files=touched_files,
            change_types=declaration.expected_change_types,
            spec_digest=spec_context.digest if spec_context else None,
            patch_excerpt=patch_excerpt,
            feedback_texts=feedback_texts,
            verification_passed=verification_passed,
        )
    except Exception:
        return []

    notes = result.notes
    if not notes:
        return []
    trust_db.add_logic_notes(
        repo_root,
        source="run_summary",
        notes=notes,
        files=touched_files,
        change_types=declaration.expected_change_types,
    )
    return notes


def _load_spec_context(repo_root: Path, spec_path: str | None, max_chars: int) -> SpecContext | None:
    if not spec_path:
        return None
    path = Path(spec_path)
    resolved = path if path.is_absolute() else (repo_root / path)
    if not resolved.exists():
        raise FileNotFoundError(f"Spec file not found: {resolved}")
    content = resolved.read_text()
    digest = _truncate_content(content, max(max_chars // 3, 1200))
    sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        display_path = str(resolved.relative_to(repo_root))
    except ValueError:
        display_path = str(resolved)
    return SpecContext(
        path=display_path,
        digest=f"{display_path} (sha256 {sha256[:12]})\n{digest}",
        sha256=sha256,
    )
