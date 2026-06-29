"""Agent security scan — the monotonic, invariant-5-safe augmentation of the
deterministic keyword security check.

Covers three layers:
  1. features.is_security_sensitive / assess_risk — the extra_security_paths
     param ADDS to the keyword floor and never clears it; default empty is
     byte-identical to the keyword-only behavior (no regression).
  2. RuleStore.set_security_paths / security_paths — per-repo round-trip,
     replace-by-source, repo isolation.
  3. The plugin scan bin (hedwig-scan.py) + decide integration — a scanned
     non-keyword file is surfaced; a prompt-injected scan cannot un-flag a
     keyword match.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sc.features import assess_risk, is_security_sensitive
from sc.trust_db import TrustDB

_PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugin" / "bin"


# --- 1. features: additive, never subtractive --------------------------------

def test_keyword_match_is_unchanged_by_default() -> None:
    """Default (no extra paths) is exactly the keyword behavior."""
    assert is_security_sensitive("svc/auth.py", "x = 1") is True   # path hint
    assert is_security_sensitive("svc/util.py", "jwt = 1") is True  # content hint
    assert is_security_sensitive("svc/util.py", "x = 1") is False   # neither


def test_extra_paths_add_a_non_keyword_file() -> None:
    """A plainly-named file the keywords miss becomes sensitive when flagged."""
    assert is_security_sensitive("svc/signing.py", "def sign(): ...") is False
    flagged = frozenset({"svc/signing.py"})
    assert is_security_sensitive("svc/signing.py", "def sign(): ...", flagged) is True


def test_extra_paths_cannot_clear_a_keyword_match() -> None:
    """Invariant 5: the agent set only ADDS. A keyword-matched file stays
    sensitive even if (absurdly) it isn't in the extra set."""
    # auth.py matches by keyword; an empty/other extra set must not declassify it.
    assert is_security_sensitive("svc/auth.py", "x=1", frozenset()) is True
    assert is_security_sensitive("svc/auth.py", "x=1", frozenset({"other.py"})) is True


def test_assess_risk_threads_extra_paths() -> None:
    repo = Path.cwd()
    base = assess_risk(repo_root=repo, file_path="svc/signing.py", old_content="",
                       new_content="def sign(): ...", is_new_file=False, diff_size=3)
    assert base.is_security_sensitive is False
    aug = assess_risk(repo_root=repo, file_path="svc/signing.py", old_content="",
                      new_content="def sign(): ...", is_new_file=False, diff_size=3,
                      extra_security_paths=frozenset({"svc/signing.py"}))
    assert aug.is_security_sensitive is True


# --- 2. store: round-trip, replace-by-source, isolation ----------------------

def test_security_paths_round_trip(tmp_path: Path) -> None:
    db = TrustDB(tmp_path / "trust.db")
    repo = "/repo/a"
    assert db.security_paths(repo) == frozenset()
    n = db.set_security_paths(repo, source="agent_scan",
                              paths=["svc/signing.py", "svc/billing.py"],
                              reasons={"svc/signing.py": "HMAC"})
    assert n == 2
    assert db.security_paths(repo) == frozenset({"svc/signing.py", "svc/billing.py"})


def test_security_paths_replace_by_source(tmp_path: Path) -> None:
    """A fresh scan supersedes the prior one rather than accumulating."""
    db = TrustDB(tmp_path / "trust.db")
    repo = "/repo/a"
    db.set_security_paths(repo, source="agent_scan", paths=["old.py"])
    db.set_security_paths(repo, source="agent_scan", paths=["new.py"])
    assert db.security_paths(repo) == frozenset({"new.py"})


def test_security_paths_repo_isolation(tmp_path: Path) -> None:
    db = TrustDB(tmp_path / "trust.db")
    db.set_security_paths("/repo/a", source="agent_scan", paths=["a.py"])
    db.set_security_paths("/repo/b", source="agent_scan", paths=["b.py"])
    assert db.security_paths("/repo/a") == frozenset({"a.py"})
    assert db.security_paths("/repo/b") == frozenset({"b.py"})


def test_empty_scan_clears_nothing_unrelated(tmp_path: Path) -> None:
    db = TrustDB(tmp_path / "trust.db")
    repo = "/repo/a"
    db.set_security_paths(repo, source="agent_scan", paths=["a.py"])
    # An empty scan from a DIFFERENT source must not wipe agent_scan's entries.
    db.set_security_paths(repo, source="other", paths=[])
    assert db.security_paths(repo) == frozenset({"a.py"})


def test_two_sources_can_flag_same_path_independently(tmp_path: Path) -> None:
    """The UNIQUE key includes source, so two sources flagging the same path
    each own a row — a replace from one source can't drop the other's flag."""
    db = TrustDB(tmp_path / "trust.db")
    repo = "/repo/a"
    db.set_security_paths(repo, source="agent_scan", paths=["shared.py"])
    db.set_security_paths(repo, source="manual", paths=["shared.py"])
    assert db.security_paths(repo) == frozenset({"shared.py"})
    # agent_scan re-scans without shared.py → manual's flag must survive.
    db.set_security_paths(repo, source="agent_scan", paths=["other.py"])
    assert "shared.py" in db.security_paths(repo), "manual's flag must persist"


# --- 3. plugin scan bin + decide integration ---------------------------------

def _env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    env["HEDWIG_NO_REEXEC"] = "1"
    return env


def _scan(payload: dict, data_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-scan.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=_env(data_dir),
    )


def _decide(payload: dict, data_dir: Path, cwd: Path) -> str:
    proc = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=str(cwd), env=_env(data_dir),
    )
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return "surfaced"  # passthrough
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


def test_scan_bin_persists_and_decide_surfaces_flagged_file(tmp_path: Path) -> None:
    """End-to-end: scan flags a non-keyword file, then a decide on it surfaces
    (not auto-applied) — the gap the keyword check missed is now closed."""
    data_dir = tmp_path / "data"
    proj = tmp_path / "proj"
    (proj / "svc").mkdir(parents=True)
    (proj / "svc" / "signing.py").write_text("def sign(body):\n    return body\n")

    out = _scan({"cwd": str(proj),
                 "security_paths": [{"path": "svc/signing.py", "reason": "request signing"}],
                 "facts": ["signing lives in svc/signing.py"]}, data_dir)
    assert out.returncode == 0, out.stderr
    assert "1 security-sensitive path" in out.stdout

    verdict = _decide({
        "tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
        "tool_input": {"file_path": str(proj / "svc" / "signing.py"),
                       "old_string": "return body", "new_string": "return body + sig"},
    }, data_dir, proj)
    assert verdict != "allow", f"a scanned security file must not auto-apply, got {verdict!r}"


def test_scan_normalizes_leading_dot_slash(tmp_path: Path) -> None:
    """A flag written as './svc/x.py' must match the editor's 'svc/x.py' rel —
    the scan strips a leading ./ so decide's exact-string match still fires."""
    data_dir = tmp_path / "data"
    proj = tmp_path / "proj"
    (proj / "svc").mkdir(parents=True)
    (proj / "svc" / "x.py").write_text("def f(): pass\n")
    out = _scan({"cwd": str(proj),
                 "security_paths": [{"path": "./svc/x.py", "reason": "test"}]}, data_dir)
    assert out.returncode == 0, out.stderr
    verdict = _decide({
        "tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
        "tool_input": {"file_path": str(proj / "svc" / "x.py"),
                       "old_string": "pass", "new_string": "return 1"},
    }, data_dir, proj)
    assert verdict != "allow", f"./-prefixed flag should still surface, got {verdict!r}"


def test_scan_bin_tolerates_garbage(tmp_path: Path) -> None:
    """Malformed scan input never crashes; exits 0."""
    data_dir = tmp_path / "data"
    proc = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-scan.py")],
        input="not json {{{", capture_output=True, text=True, env=_env(data_dir),
    )
    assert proc.returncode == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
