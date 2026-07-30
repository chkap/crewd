"""Opt-in wrapper for the clean-install artifact smoke (#52).

SKIPPED by default: the harness builds artifacts, provisions fresh virtual
environments outside the checkout, and ``pip install``s each artifact (resolving
dependencies from the index), so it is too slow/networked for the deterministic
default suite. Opt in with ``CREWD_INSTALL_SMOKE=1``:

    CREWD_INSTALL_SMOKE=1 uv run pytest tests/test_clean_install_smoke.py -q

The harness lives in ``scripts/clean_install_smoke.py`` and is CI-reusable; here
we only assert its overall verdict and every required per-artifact check.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("CREWD_INSTALL_SMOKE") != "1",
    reason="clean-install smoke is opt-in; set CREWD_INSTALL_SMOKE=1 to run",
)

REQUIRED_CHECKS = {
    "install",
    "import_version",
    "import_outside_checkout",
    "cli_help",
    "cli_init",
    "cli_refresh",
    "cli_doctor",
}


def _load_harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "clean_install_smoke.py"
    spec = importlib.util.spec_from_file_location("crewd_clean_install_smoke", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clean_install_smoke_passes_for_wheel_and_sdist():
    harness = _load_harness()
    manifest = harness.full_smoke(dist_dir=None, out_path=None)

    kinds = {r["kind"] for r in manifest["results"]}
    assert kinds == {"wheel", "sdist"}, kinds

    for res in manifest["results"]:
        missing = REQUIRED_CHECKS - set(res["checks"])
        assert not missing, f"{res['kind']} missing checks: {missing}"
        unmet = [k for k, v in res["checks"].items() if not v]
        assert not unmet, f"{res['kind']} unmet checks: {unmet}\n{res.get('details')}"
        assert len(res["sha256"]) == 64

    assert manifest["passed"], manifest
    assert manifest["cleaned_up"] is True
