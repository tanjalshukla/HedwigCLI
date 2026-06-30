"""R2 — confidence-handshake tests (the anti-allowlist pillar).

The handshake lets the agent self-pause: it declares low confidence or
explicitly requests a check-in via hedwig-declare.py (skill-driven), and the
decide hook honors that by surfacing an edit it would otherwise auto-apply.

Two branches must hold:
  * compliant — a declaration tightens proceed → surfaced (self-pause honored)
  * non-compliant — no declaration → today's behavior, unchanged

And the safety invariant: the handshake is TIGHTEN-ONLY. A high-confidence
declaration never loosens; a malformed/null payload never crashes the hook and
never auto-applies something the scorer would have surfaced.

All scripts run as subprocesses with CLAUDE_PLUGIN_DATA + scrubbed PYTHONPATH,
mirroring how Claude Code invokes them and proving the zero-dep contract.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugin" / "bin"


def _env(data_dir: Path) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    env["PYTHONPATH"] = ""
    env.pop("VIRTUAL_ENV", None)
    return env


def _run(script: str, payload: dict | None, data_dir: Path, cwd: Path | None = None):
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / script)],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=_env(data_dir),
    )


def _make_proj(tmp_path: Path) -> Path:
    """A low-risk edit target that auto-applies by default (existing test file)."""
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_basic.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    return proj


def _decide(proj: Path, data_dir: Path, *, session: str, rel: str):
    return _run(
        "hedwig-decide.py",
        {
            "tool_name": "Edit",
            "cwd": str(proj),
            "session_id": session,
            "tool_input": {
                "file_path": str(proj / rel),
                "old_string": "1 + 1 == 2",
                "new_string": "2 + 2 == 4",
            },
        },
        data_dir,
        cwd=proj,
    )


def _declare(proj: Path, data_dir: Path, *, session: str, rel: str, **fields):
    payload = {"file": str(proj / rel), "session_id": session, "cwd": str(proj)}
    payload.update(fields)
    return _run("hedwig-declare.py", payload, data_dir, cwd=proj)


def test_baseline_low_risk_edit_auto_applies(tmp_path: Path) -> None:
    """Sanity: with NO declaration, the low-risk edit auto-applies. This is the
    behavior the handshake must leave untouched in the non-compliant case."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    out = _decide(proj, data_dir, session="s1", rel="tests/test_basic.py")
    assert out.stdout, "low-risk edit should auto-apply when no declaration is made"
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_self_checkin_request_forces_surface(tmp_path: Path) -> None:
    """Compliant branch: the agent requests a check-in → the edit Hedwig would
    have auto-applied is surfaced instead (self-pause honored)."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    decl = _declare(
        proj, data_dir, session="s1", rel="tests/test_basic.py",
        requesting_self_checkin=True, reason="unsure the assertion still holds",
    )
    assert decl.returncode == 0, decl.stderr
    out = _decide(proj, data_dir, session="s1", rel="tests/test_basic.py")
    assert out.stdout, f"self-checkin request should surface, got: {out.stdout!r}"
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_low_confidence_forces_surface(tmp_path: Path) -> None:
    """Compliant branch via confidence: a low self-rated confidence surfaces."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    _declare(proj, data_dir, session="s1", rel="tests/test_basic.py", confidence=0.3)
    out = _decide(proj, data_dir, session="s1", rel="tests/test_basic.py")
    assert out.stdout, "low confidence should surface the edit"
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_high_confidence_does_not_loosen(tmp_path: Path) -> None:
    """Safety invariant: a high-confidence declaration must NOT change behavior.
    The scorer already auto-applies this edit; declaring 0.99 cannot 'upgrade'
    anything (and must never loosen a surfaced verdict — tested below)."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    _declare(proj, data_dir, session="s1", rel="tests/test_basic.py", confidence=0.99)
    out = _decide(proj, data_dir, session="s1", rel="tests/test_basic.py")
    assert out.stdout, "high confidence should leave the auto-apply untouched"
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_high_confidence_cannot_loosen_a_surfaced_edit(tmp_path: Path) -> None:
    """The core safety invariant: a brand-new file surfaces by default (the
    new-file penalty). Even a maximally-confident declaration + self-checkin
    false must NOT turn that surface into an auto-apply."""
    data_dir = tmp_path / "data"
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    new_rel = "src/brand_new.py"
    payload_decl = {
        "file": str(proj / new_rel), "session_id": "s1", "cwd": str(proj),
        "confidence": 1.0, "requesting_self_checkin": False,
    }
    _run("hedwig-declare.py", payload_decl, data_dir, cwd=proj)
    out = _run(
        "hedwig-decide.py",
        {
            "tool_name": "Write",
            "cwd": str(proj),
            "session_id": "s1",
            "tool_input": {"file_path": str(proj / new_rel), "content": "x = 1\n"},
        },
        data_dir,
        cwd=proj,
    )
    assert out.stdout, "a confident declaration must never loosen a surfaced edit"
    # Still surfaced (ask), not loosened to allow — the invariant under test.
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_declaration_is_scoped_to_its_file(tmp_path: Path) -> None:
    """A declaration on one file must not affect a different file."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    (proj / "tests" / "test_other.py").write_text("def test_o():\n    assert True\n")
    _declare(proj, data_dir, session="s1", rel="tests/test_basic.py", requesting_self_checkin=True)
    # A DIFFERENT file in the same session still auto-applies.
    out = _run(
        "hedwig-decide.py",
        {
            "tool_name": "Edit",
            "cwd": str(proj),
            "session_id": "s1",
            "tool_input": {
                "file_path": str(proj / "tests/test_other.py"),
                "old_string": "assert True",
                "new_string": "assert 1",
            },
        },
        data_dir,
        cwd=proj,
    )
    assert out.stdout, "declaration on test_basic.py must not surface test_other.py"
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_malformed_declaration_never_crashes(tmp_path: Path) -> None:
    """extra='forbid'-style discipline without pydantic: junk payloads are
    dropped, never raised. The declare script always exits 0, and a subsequent
    decide is unaffected (behaves as if no declaration was made)."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)

    # Each of these must exit 0 and write nothing usable.
    for bad in (
        "not json at all",
        json.dumps([1, 2, 3]),  # not a dict
        json.dumps({"confidence": "banana"}),  # no file, junk confidence
        json.dumps({"file": str(proj / "tests/test_basic.py"), "confidence": float("nan")}),
        json.dumps({"file": str(proj / "tests/test_basic.py"), "requesting_self_checkin": "yes"}),
    ):
        proc = subprocess.run(
            ["python3", str(_PLUGIN_BIN / "hedwig-declare.py")],
            input=bad, capture_output=True, text=True, cwd=str(proj), env=_env(data_dir),
        )
        assert proc.returncode == 0, f"declare must never crash; payload={bad!r} stderr={proc.stderr}"

    # After all that junk, the edit still auto-applies (no valid declaration stuck).
    out = _decide(proj, data_dir, session="s1", rel="tests/test_basic.py")
    assert out.stdout, "malformed declarations must not surface the edit"
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_latest_declaration_wins(tmp_path: Path) -> None:
    """If the agent declares twice, the most recent wins — a later confident
    declaration overrides an earlier uncertain one (and vice versa)."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    # First: request a check-in. Then: revise to high confidence, no request.
    _declare(proj, data_dir, session="s1", rel="tests/test_basic.py", requesting_self_checkin=True)
    _declare(proj, data_dir, session="s1", rel="tests/test_basic.py",
             confidence=0.95, requesting_self_checkin=False)
    out = _decide(proj, data_dir, session="s1", rel="tests/test_basic.py")
    assert out.stdout, "the latest (confident) declaration should win → auto-apply"
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
