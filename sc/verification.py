from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    passed: bool
    output: str


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    checks: tuple[VerificationCheck, ...]
    expected_behavior: str

    def checks_json(self) -> str:
        payload = [
            {"name": check.name, "passed": check.passed, "output": check.output}
            for check in self.checks
        ]
        return json.dumps(payload)


def _run_subprocess_check(
    name: str,
    argv: list[str],
    *,
    cwd: Path,
    timeout_sec: int,
) -> VerificationCheck:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=max(timeout_sec, 1),
        )
        output = (result.stdout + result.stderr).strip()
        return VerificationCheck(name=name, passed=result.returncode == 0, output=output or "ok")
    except subprocess.TimeoutExpired:
        return VerificationCheck(name=name, passed=False, output="verification timed out")
    except Exception as exc:
        return VerificationCheck(name=name, passed=False, output=str(exc))


def run_verification(
    *,
    repo_root: Path,
    touched_files: list[str],
    expected_behavior: str,
    timeout_sec: int,
    command: str | None = None,
) -> VerificationResult:
    checks: list[VerificationCheck] = []

    if command:
        argv = shlex.split(command)
        if not argv:
            checks.append(VerificationCheck(
                name="custom_verification", passed=False, output="verification command is empty"
            ))
        else:
            checks.append(_run_subprocess_check(
                "custom_verification", argv, cwd=repo_root, timeout_sec=timeout_sec
            ))

    python_files = [path for path in touched_files if path.endswith(".py")]
    if python_files:
        checks.append(_run_subprocess_check(
            "python_syntax",
            [sys.executable, "-m", "py_compile", *python_files],
            cwd=repo_root,
            timeout_sec=timeout_sec,
        ))

    if not checks:
        checks.append(VerificationCheck(
            name="sanity", passed=True, output="no language-specific checks required"
        ))

    return VerificationResult(
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
        expected_behavior=expected_behavior,
    )
