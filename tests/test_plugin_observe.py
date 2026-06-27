"""Plugin observability surfaces — /hedwig-weights and /hedwig-retrospective.

These read what the plugin already records (the classifier blob, regret.jsonl)
and surface it. Run as subprocesses the way Claude Code invokes the slash
commands, with an isolated CLAUDE_PLUGIN_DATA.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
_PLUGIN_BIN = _PLUGIN / "bin"
_VENDOR = _PLUGIN / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def _env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    env["HEDWIG_NO_REEXEC"] = "1"
    return env


def _observe(data_dir: Path, sub: str, *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-observe.py"), sub],
        capture_output=True, text=True, cwd=str(cwd), env=_env(data_dir),
    )


def test_retrospective_lists_regret_events(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    repo_key = str(repo.resolve())
    with (data_dir / "regret.jsonl").open("w") as f:
        f.write(json.dumps({"session_id": "s1", "cwd": repo_key,
                            "files": ["src/auth.py"], "signal": "reversal"}) + "\n")
        f.write(json.dumps({"session_id": "s1", "cwd": repo_key,
                            "files": ["api/routes.py"], "verify_cmd": "pytest"}) + "\n")

    out = _observe(data_dir, "retrospective", cwd=repo)
    assert out.returncode == 0, out.stderr
    assert "2 regret events" in out.stdout
    assert "src/auth.py" in out.stdout and "reverted" in out.stdout
    assert "api/routes.py" in out.stdout and "failed verification" in out.stdout


def test_retrospective_empty_is_friendly(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _observe(data_dir, "retrospective", cwd=repo)
    assert out.returncode == 0, out.stderr
    assert "No regret events yet" in out.stdout


def test_retrospective_scopes_to_repo(tmp_path: Path) -> None:
    """A regret recorded for another repo must not show here."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    with (data_dir / "regret.jsonl").open("w") as f:
        f.write(json.dumps({"session_id": "s1", "cwd": "/some/other/repo",
                            "files": ["x.py"], "signal": "reversal"}) + "\n")
    out = _observe(data_dir, "retrospective", cwd=repo)
    assert out.returncode == 0, out.stderr
    assert "No regret events yet" in out.stdout


def test_weights_handles_no_classifier(tmp_path: Path) -> None:
    """With deps present but no classifier recorded, weights says so cleanly."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _observe(data_dir, "weights", cwd=repo)
    assert out.returncode == 0, out.stderr
    # Either "isn't active" (no deps) or "No classifier recorded" (deps, cold) —
    # both are valid clean messages; never a crash.
    assert "Traceback" not in out.stderr
    assert out.stdout.strip()


def test_weights_shows_drift_when_classifier_learned(tmp_path: Path) -> None:
    """With a materialized, drifted classifier, weights shows per-feature drift.
    Requires numpy/sklearn (the learned path); skip if unavailable here."""
    try:
        import numpy  # noqa: F401
        import sklearn  # noqa: F401
    except Exception:
        import pytest
        pytest.skip("needs numpy+sklearn for the learned path")

    from sc.ml_policy import build_cold_classifier
    from sc.policy import PolicyInput
    from sc.trust_db import TrustDB

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    repo_key = str(repo.resolve())
    db = TrustDB(data_dir / "trust.db")
    clf = build_cold_classifier()
    pi = PolicyInput(
        prior_approvals=0, prior_denials=2, avg_response_ms=None,
        avg_edit_distance=0.0, diff_size=90, blast_radius=4, is_new_file=True,
        is_security_sensitive=False, change_pattern="api_change",
        recent_denials=1, files_in_action=1,
    )
    for _ in range(12):
        clf.update(pi, approved=False)
    db.save_policy_model(repo_key, clf)

    out = _observe(data_dir, "weights", cwd=repo)
    assert out.returncode == 0, out.stderr
    assert "learned classifier drift" in out.stdout
    assert "12 real decisions" in out.stdout
    # Denial-heavy training drifts toward caution (▼).
    assert "▼" in out.stdout


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
