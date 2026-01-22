from __future__ import annotations

import subprocess
from pathlib import Path


class RepoError(RuntimeError):
    pass


def get_repo_root() -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RepoError("Not a git repository (or git not available).") from exc
    path = result.stdout.strip()
    if not path:
        raise RepoError("Unable to resolve git repo root.")
    return Path(path)
