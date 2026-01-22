from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ApprovalResult:
    approved: bool
    remembered: bool
    auto_approved: bool


def within_scope_budget(files: Iterable[str], scope_budget_files: int) -> bool:
    return len(list(files)) <= scope_budget_files
