from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["read_request"]
    files: list[str]
    reason: str | None = None

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for path in value:
            if not path or path.strip() == "":
                raise ValueError("files cannot contain empty paths")
            if Path(path).is_absolute():
                raise ValueError("files must be repo-relative")
            norm = str(Path(path))
            if norm.startswith(".."):
                raise ValueError("files must not escape repo")
            normalized.append(norm)
        return normalized


class IntentDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_summary: str
    planned_files: list[str]
    planned_actions: list[Literal["edit_code", "add_tests", "run_tests"]]
    planned_commands: list[str]
    notes: str | None = None

    @field_validator("planned_files")
    @classmethod
    def validate_planned_files(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for path in value:
            if not path or path.strip() == "":
                raise ValueError("planned_files cannot contain empty paths")
            if Path(path).is_absolute():
                raise ValueError("planned_files must be repo-relative")
            norm = str(Path(path))
            if norm.startswith(".."):
                raise ValueError("planned_files must not escape repo")
            normalized.append(norm)
        return normalized
