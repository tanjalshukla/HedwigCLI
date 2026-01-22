from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable


class PatchError(RuntimeError):
    pass


class PatchValidationError(PatchError):
    pass


def looks_like_unified_diff(text: str) -> bool:
    has_plus = any(line.startswith("+++") for line in text.splitlines())
    has_minus = any(line.startswith("---") for line in text.splitlines())
    return has_plus and has_minus


def sanitize_patch(text: str) -> str:
    if not text:
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if "```" in normalized:
        try:
            start = lines.index("```diff")
        except ValueError:
            start = lines.index("```") if "```" in lines else 0
        try:
            end = lines.index("```", start + 1)
        except ValueError:
            end = len(lines)
        lines = lines[start + 1 : end]
    start_idx = None
    for idx, line in enumerate(lines):
        if line.startswith("diff --git") or line.startswith("--- ") or line.startswith("+++ "):
            start_idx = idx
            break
    if start_idx is None:
        return "\n".join(lines).strip()
    trimmed = "\n".join(lines[start_idx:]).strip()
    if trimmed and not trimmed.endswith("\n"):
        trimmed += "\n"
    return trimmed


def _normalize_diff_path(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


def extract_touched_files(diff_text: str) -> set[str]:
    touched: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            path = line[4:].strip().split("\t", 1)[0].strip()
            if path == "/dev/null":
                continue
            path = _normalize_diff_path(path)
            if path:
                touched.add(path)
    return touched


def validate_touched_files(
    repo_root: Path, touched_files: Iterable[str], allowed_files: set[str]
) -> None:
    for path in touched_files:
        if os.path.isabs(path):
            raise PatchValidationError(f"Patch references absolute path: {path}")
        norm = os.path.normpath(path)
        if norm.startswith(".."):
            raise PatchValidationError(f"Patch path escapes repo: {path}")
        if path not in allowed_files:
            raise PatchValidationError(f"Patch touches unapproved file: {path}")
        if not (repo_root / path).resolve().is_relative_to(repo_root.resolve()):
            raise PatchValidationError(f"Patch path escapes repo: {path}")


def apply_patch(repo_root: Path, diff_text: str, check_only: bool = False) -> None:
    cmd = ["git", "apply", "--whitespace=nowarn"]
    if check_only:
        cmd.append("--check")
    result = subprocess.run(
        cmd,
        input=diff_text,
        text=True,
        capture_output=True,
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise PatchError(result.stderr.strip() or "git apply failed")
