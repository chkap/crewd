"""Network-gated wrapper for the integrated live SDK smoke.

SKIPPED by default so the deterministic suite never touches the real SDK,
network, auth, or Premium quota. Opt in with ``CREWD_LIVE_SMOKE=1`` in an
environment that has a working Copilot runtime:

    CREWD_LIVE_SMOKE=1 uv run --active pytest tests/test_live_smoke.py -q

The harness itself lives in ``scripts/live_smoke.py`` and returns a sanitized
manifest; here we only assert its overall verdict and each required check.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("CREWD_LIVE_SMOKE") != "1",
    reason="live SDK smoke is opt-in; set CREWD_LIVE_SMOKE=1 to run",
)


def _load_harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_smoke.py"
    spec = importlib.util.spec_from_file_location("crewd_live_smoke", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_integrated_live_smoke_passes():
    harness = _load_harness()
    manifest = harness.full_smoke(out_path=None)
    unmet = [k for k, v in manifest["checks"].items() if not v]
    assert manifest["passed"], f"unmet checks: {unmet}"
    assert manifest["workspaces_cleaned_up"] is True
