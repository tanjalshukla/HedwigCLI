from __future__ import annotations

from pathlib import Path


class PatchValidationError(ValueError):
    """Raised when touched files fall outside the approved set."""


def validate_touched_files(
    repo_root: Path,
    touched_files: list[str],
    allowed_files: set[str],
) -> None:
    """Raise PatchValidationError if any touched file is not in allowed_files."""
    extra = set(touched_files) - allowed_files
    if extra:
        raise PatchValidationError(
            f"Updates touch files outside the approved set: {sorted(extra)}"
        )
