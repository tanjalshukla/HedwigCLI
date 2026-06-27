"""SPINE 3 tests — outcome-based learning through trust.db (zero-dep).

The loop:
  decide (read policy_history → score) → record (persist auto-apply trace)
  → verify Stop-hook (persist negative-outcome trace on verification failure)
  → next decide on the same file tightens.

All scripts run as subprocesses with CLAUDE_PLUGIN_DATA + scrubbed PYTHONPATH,
mirroring how Claude Code invokes them and proving the zero-dep contract:
nothing here may pull numpy / sklearn / anthropic / boto. The
test_decide_with_history_is_zero_dep guard is the load-bearing one — it
exercises the decide-with-history path (not just cold import) so a stray
classifier-materializing call would surface as an ImportError on a clean
interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_PLUGIN_BIN = Path(__file__).resolve().parent.parent / "plugin" / "bin"


def _env(data_dir: Path, *, scrub: bool = True) -> dict:
    env = dict(os.environ)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    if scrub:
        env["PYTHONPATH"] = ""
        env.pop("VIRTUAL_ENV", None)
    return env


def _run(script: str, *args: str, payload: dict | None, data_dir: Path, cwd: Path | None = None, extra_env: dict | None = None):
    env = _env(data_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["python3", str(_PLUGIN_BIN / script), *args],
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _decide(proj: Path, data_dir: Path, *, session: str, rel: str, old: str, new: str):
    return _run(
        "hedwig-decide.py",
        payload={
            "tool_name": "Edit",
            "cwd": str(proj),
            "session_id": session,
            "tool_input": {"file_path": str(proj / rel), "old_string": old, "new_string": new},
        },
        data_dir=data_dir,
        cwd=proj,
    )


def _tightened(out) -> bool:
    """True if a decide result is anything OTHER than an auto-apply — i.e. the
    file did NOT silently auto-apply. Covers both forms of tightening: a
    passthrough surface (empty stdout) and an R6 deny (a gated high-risk or
    previously-regretted edit is blocked with a reason). Tests that only care
    'this no longer auto-applies' use this instead of pinning one form."""
    if out.stdout == "":
        return True  # surfaced → native prompt
    decision = json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"]
    return decision == "deny"


def _record(proj: Path, data_dir: Path, *, session: str, rel: str, old: str = "", new: str = ""):
    """Simulate PostToolUse. Real Claude Code payloads carry the executed
    tool_input (incl. old_string/new_string for Edit) — pass them so the
    recorder's reversal detection has something to match against."""
    tool_input: dict = {"file_path": str(proj / rel)}
    if old or new:
        tool_input["old_string"] = old
        tool_input["new_string"] = new
    return _run(
        "hedwig-record.py",
        payload={
            "tool_name": "Edit",
            "cwd": str(proj),
            "session_id": session,
            "tool_input": tool_input,
        },
        data_dir=data_dir,
        cwd=proj,
    )


def _git_init(proj: Path) -> None:
    """Init a git repo and commit a baseline so later edits show as a diff.

    The verify hook scopes failure attribution to the working-tree diff (R1):
    a file only takes negative signal if it's part of the failing change. The
    test proj must therefore be a real git repo with a committed baseline,
    otherwise nothing is 'changed' and (correctly) nothing is blamed.
    """
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "baseline"]):
        subprocess.run(cmd, cwd=str(proj), env=env, capture_output=True, check=True)


def _make_proj(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "calc.py").write_text("def add(a,b):\n    return a+b\n")
    _git_init(proj)
    return proj


def _apply(proj: Path, rel: str, old: str, new: str) -> None:
    """Mirror Claude Code mutating the file between PreToolUse and PostToolUse.

    The verify hook scopes blame to the working-tree diff, so a file only
    takes negative signal if it was actually changed on disk — exactly what a
    real edit does. Tests must apply the edit to reproduce that.
    """
    target = proj / rel
    target.write_text(target.read_text().replace(old, new, 1))


def test_auto_apply_records_positive_history(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a+b", new="a + b")
    rec = _record(proj, data_dir, session="s1", rel="src/calc.py")
    assert rec.returncode == 0, rec.stderr

    traces = (data_dir / "traces.jsonl").read_text()
    assert '"user_decision": "auto_approve"' in traces


def test_verification_failure_records_negative_and_tightens(tmp_path: Path) -> None:
    """The full loop: auto-apply, then a failing verification command makes
    the SAME file surface for review on the next decide."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)

    # 1. Auto-apply + record (mutate the file on disk, as Claude Code would).
    out1 = _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a+b", new="a + b")
    assert json.loads(out1.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    _apply(proj, "src/calc.py", "a+b", "a + b")
    _record(proj, data_dir, session="s1", rel="src/calc.py")

    # 2. Stop hook with a failing verification command → negative-outcome trace.
    verify = _run(
        "hedwig-verify.py",
        payload={"session_id": "s1", "cwd": str(proj)},
        data_dir=data_dir,
        cwd=proj,
        extra_env={"HEDWIG_VERIFY_CMD": "false"},  # always fails
    )
    assert verify.returncode == 0, verify.stderr
    assert (data_dir / "regret.jsonl").exists()

    # 3. Same file, next edit → no longer auto-applies. The prior denial both
    #    tightens the score AND trips R6's "previously regretted" deny gate, so
    #    this is now blocked with a reason (a stronger tightening than a
    #    passthrough surface). Either form counts as "did not auto-apply".
    out2 = _decide(proj, data_dir, session="s2", rel="src/calc.py", old="a + b", new="a  +  b")
    assert _tightened(out2), f"expected tightened (surface or deny), got: {out2.stdout!r}"


def test_tightening_is_per_file_not_global(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    (proj / "src" / "other.py").write_text("x=1\n")

    # Taint calc.py with a failed-verification outcome (apply on disk so it's
    # in the failing change the verify hook scopes to).
    _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a+b", new="a + b")
    _apply(proj, "src/calc.py", "a+b", "a + b")
    _record(proj, data_dir, session="s1", rel="src/calc.py")
    _run(
        "hedwig-verify.py",
        payload={"session_id": "s1", "cwd": str(proj)},
        data_dir=data_dir, cwd=proj, extra_env={"HEDWIG_VERIFY_CMD": "false"},
    )

    # A different, untainted file still auto-applies.
    out = _decide(proj, data_dir, session="s2", rel="src/other.py", old="x=1", new="x = 1")
    assert out.stdout, "untainted file should still auto-apply"
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_decide_with_history_is_zero_dep(tmp_path: Path) -> None:
    """LOAD-BEARING GUARD. Exercise the decide path AFTER history exists in
    trust.db, on a clean interpreter (scrubbed PYTHONPATH, no venv). If any
    code on this path materializes a PolicyClassifier, it lazily imports
    ml_policy → numpy/sklearn, which won't be importable here, and the
    subprocess fails. Cold-import tests don't catch this; this does.
    """
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)

    # Build real history first.
    _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a+b", new="a + b")
    _record(proj, data_dir, session="s1", rel="src/calc.py")

    # Now decide again — this reads policy_history + the classifier from
    # trust.db, and imports rule_store→sc.retrieval (R3). On THIS scrubbed
    # interpreter numpy/sklearn/fastembed are unavailable, so the classifier
    # load and the EmbeddingRanker MUST both degrade silently to the
    # stdlib-only heuristic / KeywordRanker — never crash the hook (S5: the
    # learned path is best-effort; a clean install ships the deps, but a hook
    # must survive their absence). The decide path must still emit a verdict.
    proc = _decide(proj, data_dir, session="s2", rel="src/calc.py", old="a + b", new="a  +  b")
    assert proc.returncode == 0, proc.stderr
    # The forbidden deps must NEVER appear, even as a failed import in stderr.
    err = proc.stderr.lower()
    assert "torch" not in err
    assert "anthropic" not in err
    assert "boto" not in err
    # Degradation must be silent: no unhandled ModuleNotFound surfaced.
    assert "modulenotfound" not in err
    assert "traceback" not in err


def test_decide_degrades_when_sklearn_unimportable(tmp_path: Path) -> None:
    """S5 safety net — NOT a vacuous test. sklearn is installed system-wide, so
    a merely-scrubbed PYTHONPATH still finds it (the G2 trap the doc warns of).
    Here we SHADOW sklearn with a package that raises ImportError, simulating a
    genuine clean machine without it, and prove the decide hook degrades to the
    stdlib heuristic — emits a valid verdict, exits 0, never crashes — rather
    than wedging the edit. The classifier load is best-effort by contract.
    """
    # A fake `sklearn` that raises the moment it's imported.
    fakelib = tmp_path / "fakelib"
    (fakelib / "sklearn").mkdir(parents=True)
    (fakelib / "sklearn" / "__init__.py").write_text(
        'raise ImportError("sklearn blocked for degradation test")\n'
    )
    proj = _make_proj(tmp_path)

    # Sanity: with fakelib first on the path, sklearn really is unimportable.
    probe = subprocess.run(
        ["python3", "-c", "import sklearn"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(fakelib)},
    )
    assert probe.returncode != 0 and "blocked" in probe.stderr, (
        "test setup failed: sklearn was not actually shadowed"
    )

    data_dir = tmp_path / "data"
    env = _env(data_dir, scrub=False)
    env["PYTHONPATH"] = str(fakelib)  # shadow sklearn but keep the rest of the env
    proc = subprocess.run(
        ["python3", str(_PLUGIN_BIN / "hedwig-decide.py")],
        input=json.dumps({
            "tool_name": "Edit",
            "cwd": str(proj),
            "session_id": "s1",
            "tool_input": {
                "file_path": str(proj / "src/calc.py"),
                "old_string": "a+b",
                "new_string": "a + b",
            },
        }),
        capture_output=True, text=True, cwd=str(proj), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "decide must still emit a verdict when sklearn is absent"
    decision = json.loads(proc.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "traceback" not in proc.stderr.lower()


def _load_verify_module():
    """Import plugin/bin/hedwig-verify.py as a module to test _changed_files."""
    import importlib.util
    path = _PLUGIN_BIN / "hedwig-verify.py"
    spec = importlib.util.spec_from_file_location("hedwig_verify_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_changed_files_handles_git_rename(tmp_path: Path) -> None:
    """git status --porcelain -z emits a rename as 'R  new\\0old' — two
    \\0-delimited fields for one record. The parser must consume the source
    field, not slice [3:] off the bare old path (which drops its first 3 chars
    and loses the real old name). Otherwise a verification-failure regret on a
    renamed auto-applied file is mis-attributed."""
    verify = _load_verify_module()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "torename.py").write_text("x = 1\n")
    (proj / "untouched.py").write_text("y = 2\n")
    _git_init(proj)

    # Rename via git so it shows as a staged rename (status R).
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "mv", "torename.py", "renamed.py"],
                   cwd=str(proj), env=env, capture_output=True, check=True)

    changed = verify._changed_files(str(proj))
    assert changed is not None
    # The new path is present and uncorrupted; no mangled 3-char-stripped entry.
    assert "renamed.py" in changed, f"new path missing/corrupt: {changed}"
    assert "ename.py" not in changed, f"corrupted source-path slice leaked in: {changed}"
    # untouched.py wasn't part of the change → must not appear.
    assert "untouched.py" not in changed


def test_ml_policy_is_vendored() -> None:
    """S5 INVERTED the old wall: ml_policy.py (the online log-reg
    PolicyClassifier) IS the contribution and MUST ship in the default plugin.
    The earlier 'exclude it to stay zero-dep' decision was reversed — R3's
    fastembed already pulled numpy, so the marginal cost of sklearn is only
    scipy+joblib (no torch). If sync_vendor.py drops it, the learned scorer is
    silently gone from the plugin and this fails, forcing an explicit decision.
    """
    vendor_sc = _PLUGIN_BIN.parent / "vendor" / "sc"
    assert vendor_sc.is_dir(), "vendored sc/ missing — run plugin/sync_vendor.py"
    assert (vendor_sc / "ml_policy.py").exists(), (
        "ml_policy.py is NOT vendored — the online log-reg classifier (the core "
        "novelty) won't ship in the default plugin. Add 'ml_policy.py' to "
        "VENDORED_MODULES in plugin/sync_vendor.py and re-run it."
    )
    # The dependency wall MOVED, it didn't vanish: numpy/sklearn/fastembed are
    # allowed on the decide path; torch/anthropic/boto must never be vendored.
    for forbidden in ("torch", "anthropic", "boto", "boto3", "botocore"):
        assert not (vendor_sc / forbidden).exists(), f"{forbidden} must not be vendored"


def test_retrieval_is_vendored() -> None:
    """R3 made rule_store.py import sc.retrieval at module scope. rule_store is
    in the live decide closure (TrustDB inherits RuleStoreMixin), so retrieval
    MUST be vendored too or the standalone plugin install fails with
    ModuleNotFoundError. retrieval's top-level imports are stdlib-only
    (fastembed is lazy), so vendoring it keeps Tier-0 zero-dep — the
    EmbeddingRanker default degrades to KeywordRanker when fastembed is absent.
    If sync_vendor.py drops it, this fails and forces an explicit decision."""
    vendor_sc = _PLUGIN_BIN.parent / "vendor" / "sc"
    assert (vendor_sc / "retrieval.py").exists(), (
        "sc/retrieval.py is NOT vendored but rule_store.py imports it at module "
        "scope — the standalone plugin decide path will crash. Add 'retrieval.py' "
        "to VENDORED_MODULES in plugin/sync_vendor.py and re-run it."
    )
    # And the embedding dep must NOT be vendored — it stays a research-repo /
    # Tier-1 install concern, never bundled into the plugin tree.
    assert not (vendor_sc / "fastembed").exists()


# --- R1: outcome-signal attribution -----------------------------------------


def test_verification_failure_does_not_blame_unrelated_clean_file(tmp_path: Path) -> None:
    """ADVERSARIAL (R1 fix a). Two files auto-applied this session; only ONE
    is part of the failing change. The clean, unrelated file must NOT take a
    negative signal — it must still auto-apply next time. Pre-R1 this failed:
    a verification failure blamed every auto-applied file in the session."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)
    (proj / "src" / "other.py").write_text("x=1\n")
    _git_init(proj)  # commit other.py too so only deliberate edits show as diff

    # Auto-apply on BOTH files this session, but only mutate calc.py on disk —
    # other.py is recorded as auto-applied but left clean (not in the diff).
    _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a+b", new="a + b")
    _apply(proj, "src/calc.py", "a+b", "a + b")
    _record(proj, data_dir, session="s1", rel="src/calc.py")
    _decide(proj, data_dir, session="s1", rel="src/other.py", old="x=1", new="x = 1")
    _record(proj, data_dir, session="s1", rel="src/other.py")  # no _apply → clean

    # Verification fails. Only calc.py is in the working-tree diff.
    _run(
        "hedwig-verify.py",
        payload={"session_id": "s1", "cwd": str(proj)},
        data_dir=data_dir, cwd=proj, extra_env={"HEDWIG_VERIFY_CMD": "false"},
    )

    # calc.py (in the failing change) no longer auto-applies (surface or deny)…
    tainted = _decide(proj, data_dir, session="s2", rel="src/calc.py", old="a + b", new="a  +  b")
    assert _tightened(tainted), "file in the failing change should be tightened"
    # …but other.py (clean, unrelated) must STILL auto-apply.
    clean = _decide(proj, data_dir, session="s2", rel="src/other.py", old="x = 1", new="x  =  1")
    assert clean.stdout, "unrelated clean file must NOT be blamed for the failure"
    assert json.loads(clean.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_reversal_is_negative_signal_without_verify_command(tmp_path: Path) -> None:
    """R1 fix b + demo money-shot. With NO HEDWIG_VERIFY_CMD configured: an
    edit auto-applies, the agent reverts it (B→A), and the next like-action on
    that file tightens — purely from the reversal, no verification needed."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)

    # 1. Auto-apply a+b → a + b, agent applies it on disk, record positive.
    out1 = _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a+b", new="a + b")
    assert json.loads(out1.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    _apply(proj, "src/calc.py", "a+b", "a + b")
    _record(proj, data_dir, session="s1", rel="src/calc.py")

    # 2. Agent reverts: a + b → a+b (exact inverse of the auto-applied edit).
    _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a + b", new="a+b")
    _record(proj, data_dir, session="s1", rel="src/calc.py", old="a + b", new="a+b")
    # The reversal is recorded as a regret, with NO verify command anywhere.
    assert (data_dir / "regret.jsonl").exists(), "reversal must record a regret"
    regret = (data_dir / "regret.jsonl").read_text()
    assert '"signal": "reversal"' in regret
    traces = (data_dir / "traces.jsonl").read_text()
    assert '"signal": "reversal"' in traces  # recorded as deny, not a fresh positive

    # 3. Next like-action on calc.py no longer auto-applies (the reversal both
    #    tightens the score and trips R6's prior-regret deny gate).
    nxt = _decide(proj, data_dir, session="s2", rel="src/calc.py", old="a+b", new="a  +  b")
    assert _tightened(nxt), f"reverted file should tighten next time, got: {nxt.stdout!r}"


def test_routine_followup_edit_is_not_a_reversal(tmp_path: Path) -> None:
    """ADVERSARIAL. A non-inverse follow-up edit must NOT be mistaken for a
    reversal — only an exact inverse of the auto-applied edit counts, so a
    clean file isn't tightened without cause."""
    data_dir = tmp_path / "data"
    proj = _make_proj(tmp_path)

    _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a+b", new="a + b")
    _apply(proj, "src/calc.py", "a+b", "a + b")
    _record(proj, data_dir, session="s1", rel="src/calc.py", old="a+b", new="a + b")

    # A DIFFERENT follow-up edit (not the inverse) — refining, not reverting.
    _decide(proj, data_dir, session="s1", rel="src/calc.py", old="a + b", new="a + b  # ok")
    _record(proj, data_dir, session="s1", rel="src/calc.py", old="a + b", new="a + b  # ok")

    # No reversal regret was recorded.
    regret_path = data_dir / "regret.jsonl"
    if regret_path.exists():
        assert '"signal": "reversal"' not in regret_path.read_text()
    # And the file still auto-applies (two positives, no denial).
    nxt = _decide(proj, data_dir, session="s2", rel="src/calc.py", old="a+b", new="a - b")
    assert nxt.stdout, "a non-reverting follow-up must not taint the file"
    assert json.loads(nxt.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
