from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .phase import WorkflowPhase


ChangeType = Literal[
    "general_change",
    "documentation",
    "test_generation",
    "config_change",
    "api_change",
    "data_model_change",
    "dependency_update",
    "error_handling",
]
ConstraintPolicy = Literal["always_allow", "always_check_in", "always_deny"]


def _normalize_repo_paths(paths: list[str], field_name: str) -> list[str]:
    normalized: list[str] = []
    for path in paths:
        if not path or path.strip() == "":
            raise ValueError(f"{field_name} cannot contain empty paths")
        if Path(path).is_absolute():
            raise ValueError(f"{field_name} must be repo-relative")
        norm = str(Path(path))
        if norm.startswith(".."):
            raise ValueError(f"{field_name} must not escape repo")
        normalized.append(norm)
    return normalized


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["read_request"]
    files: list[str]
    reason: str | None = None

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: list[str]) -> list[str]:
        return _normalize_repo_paths(value, "files")


class IntentDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_summary: str
    planned_files: list[str]
    planned_actions: list[Literal["edit_code", "add_tests", "run_tests"]]
    planned_commands: list[str]
    workflow_phase: WorkflowPhase | None = None
    notes: str | None = None
    expected_change_types: list[ChangeType] = Field(default_factory=list)
    requirements_covered: list[str] = Field(default_factory=list)
    potential_deviations: list[str] = Field(default_factory=list)

    @field_validator("planned_files")
    @classmethod
    def validate_planned_files(cls, value: list[str]) -> list[str]:
        return _normalize_repo_paths(value, "planned_files")


class CheckInMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["check_in"]
    reason: str
    check_in_type: Literal[
        "plan_review",
        "decision_point",
        "progress_update",
        "deviation_notice",
        "phase_transition",
        "uncertainty",
    ]
    content: str
    recommendation: str | None = None
    options: list[str] | None = None
    assumptions: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class CompiledConstraintProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path_pattern: str
    read_policy: ConstraintPolicy
    write_policy: ConstraintPolicy
    reason: str | None = None

    @field_validator("path_pattern")
    @classmethod
    def validate_path_pattern(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise ValueError("path_pattern cannot be empty")
        if Path(normalized).is_absolute() or normalized.startswith("/"):
            raise ValueError("path_pattern must be repo-relative")
        parts = [part for part in normalized.split("/") if part]
        if any(part == ".." for part in parts):
            raise ValueError("path_pattern must not escape repo")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.endswith("/"):
            normalized = normalized + "*"
        return normalized


class RuleCompilation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraints: list[CompiledConstraintProposal] = Field(default_factory=list)
    behavioral_guidelines: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)

    @field_validator("behavioral_guidelines", "unresolved")
    @classmethod
    def validate_text_items(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            text = " ".join(item.split()).strip()
            if text:
                normalized.append(text)
        return normalized


class LogicNoteCompilation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: list[str] = Field(default_factory=list)

    @field_validator("notes")
    @classmethod
    def validate_notes(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            text = " ".join(item.split()).strip()
            if text:
                normalized.append(text[:280])
        return normalized[:3]


class AutonomyRationale(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rationale: str | None = None

    @field_validator("rationale")
    @classmethod
    def validate_rationale(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(value.split()).strip()
        if not text:
            return None
        return text[:180]
