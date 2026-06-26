"""Tests for the Omnigent integration spike (R4).

Proves three things the task contract requires:
  1. The Hedwig -> Omnigent verdict mapping is correct for each action.
  2. The decision is DEMONSTRABLY history-driven: the same action returns
     ALLOW with clean trace history and ASK after a recorded denial/regret on
     that file (NOT a static rule).
  3. A malformed / empty Omnigent event never crashes the policy — it degrades
     to ASK (the safe default).

Omnigent itself is alpha and external; we do not import it. We construct the
event dicts against the interface documented in integrations/omnigent/README.md
(verified from their `main` source on 2026-06-25) and stub the repo via a tmp
dir + a real Hedwig trust.db. See README "What QC must verify live".
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from sc.trust_db import TrustDB

# Load the adapter by path (integrations/ is not a package on the test path).
_ADAPTER_PATH = Path(__file__).resolve().parents[1] / "integrations" / "omnigent" / "policy.py"
_spec = importlib.util.spec_from_file_location("omnigent_policy", _ADAPTER_PATH)
policy = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(policy)


def _seed_denial(db: TrustDB, repo_root: str, file_path: str) -> None:
    """Record a denial trace for a file via the real TraceStore — this is the
    same write path the plugin's regret/reversal recorder uses."""
    db.record_trace(
        repo_root=repo_root,
        session_id="seed",
        task="seed",
        stage="apply",
        action_type="apply",
        file_path=file_path,
        change_type="general_change",
        diff_size=10,
        blast_radius=1,
        existing_lease=False,
        lease_type=None,
        prior_approvals=0,
        prior_denials=0,
        policy_action="proceed",
        policy_score=0.0,
        user_decision="deny",
    )


def _tool_event(repo_root: Path, rel: str, *, new_content: str = "x = 1\n") -> dict:
    """An Omnigent tool_call event, per the documented PolicyEvent shape."""
    return {
        "type": "tool_call",
        "target": "write_file",
        "data": {
            "name": "write_file",
            "arguments": {"path": rel, "content": new_content},
        },
        "context": {"cwd": str(repo_root)},
    }


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch):
    """A tmp repo with a real existing file and a trust.db wired via env."""
    db_path = tmp_path / ".sc" / "trust.db"
    monkeypatch.setenv("HEDWIG_TRUST_DB", str(db_path))
    # An existing, low-risk file so the clean-history case is a clear ALLOW.
    (tmp_path / "util.py").write_text("def helper():\n    return 1\n")
    return tmp_path, str(tmp_path), db_path


def test_verdict_mapping_is_one_to_one():
    assert policy._VERDICT["proceed"] == "ALLOW"
    assert policy._VERDICT["proceed_flag"] == "ALLOW"
    assert policy._VERDICT["check_in"] == "ASK"


def test_clean_history_allows_low_risk_edit(repo):
    repo_root, repo_str, _db = repo
    event = _tool_event(repo_root, "util.py", new_content="def helper():\n    return 2\n")
    resp = policy.decide(event)
    assert resp is not None
    assert resp["result"] == "ALLOW"


def test_recorded_denial_flips_same_action_to_ask(repo):
    """The money-shot: SAME action, ALLOW before a denial, ASK after.

    This proves the decision is driven by trace history, not a static rule.
    """
    repo_root, repo_str, db_path = repo
    event = _tool_event(repo_root, "util.py", new_content="def helper():\n    return 2\n")

    # Clean history -> ALLOW.
    first = policy.decide(event)
    assert first["result"] == "ALLOW"

    # Record a denial/regret on this exact file.
    db = TrustDB(db_path)
    for _ in range(3):
        _seed_denial(db, repo_str, "util.py")

    # SAME action now surfaces (ASK) — history alone moved the verdict.
    second = policy.decide(event)
    assert second["result"] == "ASK"
    assert "cautious" in second["reason"].lower()


def test_denial_is_scoped_to_the_file(repo):
    """Tightening on one file must not taint a different clean file."""
    repo_root, repo_str, db_path = repo
    (repo_root / "other.py").write_text("def other():\n    return 1\n")

    db = TrustDB(db_path)
    for _ in range(3):
        _seed_denial(db, repo_str, "util.py")

    other_event = _tool_event(repo_root, "other.py", new_content="def other():\n    return 2\n")
    resp = policy.decide(other_event)
    assert resp["result"] == "ALLOW"  # clean file unaffected by util.py's denial


def test_malformed_event_degrades_to_ask(repo):
    """A tool_call event we cannot parse surfaces as ASK, never crashes."""
    for bad in (
        {"type": "tool_call"},  # no data
        {"type": "tool_call", "data": {}},  # no arguments
        {"type": "tool_call", "data": {"arguments": {}}},  # no path
        {"type": "tool_call", "data": {"arguments": {"path": ""}}},  # empty path
        {},  # empty event
        "not a dict",  # wrong type entirely
        None,
    ):
        resp = policy.decide(bad)
        # Either abstain (None) on a clearly non-tool/empty event, or ASK —
        # never ALLOW, never raise.
        assert resp is None or resp["result"] == "ASK"


def test_non_tool_phase_abstains(repo):
    """On phases Hedwig does not govern, abstain (None) so Omnigent's other
    policies decide — do not force ASK on, e.g., an LLM request phase."""
    assert policy.decide({"type": "llm_request", "data": {}}) is None
    assert policy.decide({"type": "response", "data": {}}) is None


def test_security_sensitive_new_file_surfaces(repo):
    """A risky action (security-sensitive + new file) surfaces even with no
    history — the risk signals alone are enough."""
    repo_root, _repo_str, _db = repo
    event = _tool_event(
        repo_root,
        "auth_token.py",  # security-sensitive path, does not exist -> new file
        new_content="SECRET_KEY = 'abc'\njwt_token = sign()\n",
    )
    resp = policy.decide(event)
    assert resp["result"] == "ASK"
