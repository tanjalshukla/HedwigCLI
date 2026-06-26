"""S5 — the online log-reg classifier on the default plugin path.

These exercise the classifier cascade IN-PROCESS (the test interpreter has
numpy/sklearn via the venv; the scrubbed subprocess tests in
test_plugin_outcome_loop.py cover the *degradation* path when those deps are
absent). We verify the CAIS cascade the plugin now ships:

  * select_scorer returns the heuristic until MIN_SAMPLES_FOR_LEARNED real
    decisions, then the learned classifier (the ready() gate);
  * a regret routed through classifier.update(approved=False) is the corrective
    gradient, fires exactly once per regret_key, and — unlike per-file history
    — generalizes to risk-signal-similar edits on OTHER files (what the
    classifier buys over the heuristic).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"
_VENDOR = _PLUGIN / "vendor"

# Make the vendored sc importable, exactly as the bins do at runtime.
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

pytest.importorskip("sklearn", reason="learned-path test needs sklearn (shipped in a real install)")
pytest.importorskip("numpy")


def _load_common():
    """Import plugin/bin/_hedwig_common.py as a module (it's not a package)."""
    path = _PLUGIN / "bin" / "_hedwig_common.py"
    spec = importlib.util.spec_from_file_location("hedwig_common_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db(common, tmp_path: Path):
    from sc.trust_db import TrustDB
    return TrustDB(tmp_path / "trust.db")


def _pi(common, *, denials=0, approvals=0.0, diff=5, blast=1, pattern="general_change"):
    from sc.policy import PolicyInput
    return PolicyInput(
        prior_approvals=approvals,
        prior_denials=denials,
        avg_response_ms=None,
        avg_edit_distance=0.0,
        diff_size=diff,
        blast_radius=blast,
        is_new_file=False,
        is_security_sensitive=False,
        change_pattern=pattern,
        recent_denials=0,
        files_in_action=1,
    )


def test_cold_start_uses_heuristic_then_learned(tmp_path: Path) -> None:
    common = _load_common()
    db = _db(common, tmp_path)
    repo = str(tmp_path)

    classifier = common.load_classifier(db, repo)
    assert classifier is not None, "a clean install must build+persist a cold classifier"

    # Cold: ready() is False, select returns the heuristic.
    _, label = common.select_active_scorer(classifier)
    assert label == "heuristic"

    # Feed MIN_SAMPLES_FOR_LEARNED real decisions; then it flips to learned.
    from sc.ml_policy import MIN_SAMPLES_FOR_LEARNED
    for _ in range(MIN_SAMPLES_FOR_LEARNED):
        classifier.update(_pi(common), approved=True)
    _, label = common.select_active_scorer(classifier)
    assert label == "learned", "after >= MIN_SAMPLES the learned scorer must take over"


def test_regret_update_fires_once_per_key(tmp_path: Path) -> None:
    common = _load_common()
    db = _db(common, tmp_path)
    repo = str(tmp_path)
    common.load_classifier(db, repo)

    pi = _pi(common, diff=10, blast=2)
    key = "reversal:s1:src/a.py"
    common.update_classifier_for_regret(db, repo, pi, regret_key=key)
    after_first = db.load_policy_model(repo)
    assert key in after_first._corrected_regret_ids

    # A second call with the same key must be a no-op (the gradient fires once).
    coef_before = after_first.clf.coef_[0].copy()
    common.update_classifier_for_regret(db, repo, pi, regret_key=key)
    after_second = db.load_policy_model(repo)
    assert list(after_second.clf.coef_[0]) == list(coef_before), (
        "the same regret key must not re-apply the negative gradient"
    )


def test_regret_generalizes_across_files(tmp_path: Path) -> None:
    """The thing the classifier buys over per-file history: a regret on one
    file shifts the learned score for a risk-signal-SIMILAR edit on a DIFFERENT
    (never-before-seen) file. Per-file history alone cannot do this."""
    common = _load_common()
    db = _db(common, tmp_path)
    repo = str(tmp_path)

    # Make the classifier learned (>= MIN_SAMPLES), all approvals so the
    # baseline score for our feature profile is high.
    classifier = common.load_classifier(db, repo)
    from sc.ml_policy import MIN_SAMPLES_FOR_LEARNED
    profile = dict(diff=40, blast=4, pattern="api_change")
    for _ in range(MIN_SAMPLES_FOR_LEARNED):
        classifier.update(_pi(common, **profile), approved=True)
    db.save_policy_model(repo, classifier)

    # Score a like-profile action on a brand-new file BEFORE any regret.
    learned = db.load_policy_model(repo)
    score_before = learned.score(_pi(common, **profile))

    # Now a regret with the SAME risk profile, attributed to a totally
    # different file. Apply several so the gradient is measurable.
    for i in range(5):
        common.update_classifier_for_regret(
            db, repo, _pi(common, **profile), regret_key=f"reversal:s1:other_{i}.py"
        )

    learned_after = db.load_policy_model(repo)
    score_after = learned_after.score(_pi(common, **profile))

    assert score_after < score_before, (
        "a regret on one file must lower the learned approval score for a "
        "risk-similar edit on a different file (cross-file generalization)"
    )


def test_load_classifier_persists_cold_model(tmp_path: Path) -> None:
    """The first load builds AND persists a cold classifier, so a subsequent
    load returns the same (not a fresh) model — the hook is stateless per call
    and relies on SQLite to carry the model between turns."""
    common = _load_common()
    db = _db(common, tmp_path)
    repo = str(tmp_path)
    assert db.load_policy_model(repo) is None
    common.load_classifier(db, repo)
    assert db.load_policy_model(repo) is not None, "cold classifier must be persisted on first load"
