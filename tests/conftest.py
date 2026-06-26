"""Test-suite-wide fixtures.

Pin the learned-interpreter re-exec shim OFF for every test. The plugin hooks
re-exec themselves under a deps-capable interpreter (~/.hedwig/venv or
$HEDWIG_PYTHON) so the classifier always runs at the booth — but in the test
suite that would be non-hermetic: a developer with HEDWIG_PYTHON set, or a
machine that happens to have the setup venv, would silently run the hooks under
a *different* interpreter than the test intends. In particular the
degradation tests (test_decide_degrades_when_sklearn_unimportable) deliberately
shadow sklearn to force the heuristic path, and a re-exec would escape that.

Setting HEDWIG_NO_REEXEC makes every hook stay on the interpreter the test
launched it under; clearing HEDWIG_PYTHON removes the other trigger. Both are
inherited by the subprocess hooks because the test harness builds their env
from os.environ.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _pin_hook_interpreter(monkeypatch):
    monkeypatch.setenv("HEDWIG_NO_REEXEC", "1")
    monkeypatch.delenv("HEDWIG_PYTHON", raising=False)
    yield
