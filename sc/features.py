from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RiskSignals:
    """Raw risk signals for one action. Consumers weight as they see fit."""

    change_pattern: str
    blast_radius: int
    is_security_sensitive: bool
    is_new_file: bool
    diff_size: int


# Canonical change-pattern vocabulary. Scorers derive their weights from these;
# features.py is the single source of truth for what the categories are.
CHANGE_PATTERNS: tuple[str, ...] = (
    "api_change",
    "data_model_change",
    "config_change",
    "dependency_update",
    "error_handling",
    "test_generation",
    "documentation",
    "general_change",
)


_SECURITY_PATH_HINTS = (
    "auth",
    "permission",
    "token",
    "secret",
    "password",
    "credential",
    "crypto",
    "iam",
)
_SECURITY_CONTENT_HINTS = (
    "authorization",
    "jwt",
    "oauth",
    "apikey",
    "access_key",
    "secret_key",
    "password",
    "encrypt",
    "decrypt",
)


def is_security_sensitive(file_path: str, content: str) -> bool:
    lower_path = file_path.lower()
    if any(hint in lower_path for hint in _SECURITY_PATH_HINTS):
        return True
    lower_content = content.lower()
    return any(hint in lower_content for hint in _SECURITY_CONTENT_HINTS)


def classify_change_pattern(file_path: str, old_content: str, new_content: str) -> str:
    path_lower = file_path.lower()
    if "test" in path_lower or path_lower.endswith("_test.py") or "/tests/" in path_lower:
        return "test_generation"
    if path_lower.endswith((".md", ".rst", ".txt")):
        return "documentation"
    if path_lower.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg")):
        return "config_change"
    if any(segment in path_lower for segment in ("/api/", "router", "endpoint", "routes")):
        return "api_change"
    if any(segment in path_lower for segment in ("schema", "migration", "model")):
        return "data_model_change"

    old_lower = old_content.lower()
    new_lower = new_content.lower()
    if ("try:" in new_lower or "except " in new_lower) and ("try:" not in old_lower and "except " not in old_lower):
        return "error_handling"
    if "import " in new_lower and "import " not in old_lower:
        return "dependency_update"
    return "general_change"


def change_type_label(risk: RiskSignals) -> str:
    """Stable string form used in traces and milestone checks. Preserves the
    legacy ``new_file:`` prefix so persisted decision_traces stay parseable."""
    prefix = "new_file:" if risk.is_new_file else ""
    return f"{prefix}{risk.change_pattern}"


def assess_risk(
    *,
    repo_root: Path,
    file_path: str,
    old_content: str,
    new_content: str,
    is_new_file: bool,
    diff_size: int,
) -> RiskSignals:
    """Single entry point for assessing one action's risk."""
    return RiskSignals(
        change_pattern=classify_change_pattern(file_path, old_content, new_content),
        blast_radius=estimate_blast_radius(repo_root, file_path),
        is_security_sensitive=is_security_sensitive(file_path, new_content),
        is_new_file=is_new_file,
        diff_size=diff_size,
    )


def estimate_blast_radius(repo_root: Path, file_path: str) -> int:
    target = Path(file_path)
    if target.suffix != ".py":
        return 1
    module_name = target.stem
    if not module_name:
        return 1

    pattern = re.compile(rf"(from\s+[\w\.]*{re.escape(module_name)}\s+import|import\s+[\w\.,\s]*\b{re.escape(module_name)}\b)")
    count = 0
    for candidate in repo_root.rglob("*.py"):
        rel = str(candidate.relative_to(repo_root))
        if rel == file_path:
            continue
        try:
            text = candidate.read_text()
        except Exception:
            continue
        if pattern.search(text):
            count += 1
    return max(count, 1)
