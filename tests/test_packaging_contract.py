"""Packaging/default-backend alignment contract (#26).

The default (and only production) backend is ``copilot-sdk``, which imports the
``copilot`` package shipped by ``github-copilot-sdk``. A new operator following
the canonical quickstart runs a plain ``uv sync`` / ``pip install crewd`` and
must land in a *runnable* environment — so the SDK must be a required core
dependency, never hidden behind an optional extra. These deterministic checks
lock that contract (and the remediation diagnostic) so the #25 install defect
cannot silently regress even if README examples change.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from crewd.config import CrewConfig

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject() -> dict:
    with open(PYPROJECT, "rb") as f:
        return tomllib.load(f)


def _core_deps() -> list[str]:
    return _pyproject()["project"].get("dependencies", [])


def _default_backend() -> str:
    return CrewConfig.model_fields["backend"].default


def test_default_backend_is_copilot_sdk():
    assert _default_backend() == "copilot-sdk"


def test_sdk_is_a_required_core_dependency():
    # The default backend imports `copilot`; the distribution that provides it
    # must be declared in core dependencies so a plain install is runnable.
    core = _core_deps()
    assert any(d.replace(" ", "").startswith("github-copilot-sdk") for d in core), (
        "github-copilot-sdk must be a core dependency for the default "
        f"copilot-sdk backend; core dependencies were: {core}"
    )


def test_sdk_not_gated_behind_optional_extra():
    # Regression guard for #25/#26: the SDK must not be reachable *only* via an
    # optional extra while the default backend requires it. If any optional
    # extra exists, it must not be the sole home of github-copilot-sdk.
    proj = _pyproject()["project"]
    extras = proj.get("optional-dependencies", {})
    in_core = any(
        d.replace(" ", "").startswith("github-copilot-sdk") for d in _core_deps()
    )
    assert in_core, "SDK must be in core dependencies, not only an optional extra"
    for name, deps in extras.items():
        for d in deps:
            assert not d.replace(" ", "").startswith("github-copilot-sdk"), (
                f"github-copilot-sdk must not be gated behind optional extra "
                f"'{name}'; it is a required core dependency"
            )


def test_wheel_build_config_has_no_duplicate_template_include():
    # The wheel packages `src/crewd`, which already contains `templates/`. A
    # `force-include` mapping `src/crewd/templates` -> `crewd/templates` (as
    # existed pre-#26) makes hatchling add every template file twice and fails
    # the wheel build ("A second file is being added ... at the same path").
    # Guard: no force-include may duplicate a path already inside a packaged dir.
    wheel_cfg = (
        _pyproject().get("tool", {}).get("hatch", {})
        .get("build", {}).get("targets", {}).get("wheel", {})
    )
    packages = wheel_cfg.get("packages", [])
    force_include = wheel_cfg.get("force-include", {})
    for src in force_include:
        for pkg in packages:
            assert not src.startswith(pkg.rstrip("/") + "/"), (
                f"force-include '{src}' duplicates content already packaged via "
                f"'{pkg}' — this breaks `uv build --wheel`"
            )


def test_missing_sdk_diagnostic_names_correct_remediation(monkeypatch):
    """When the SDK is unimportable, doctor must name the real install path and
    must NOT recommend the retired `crewd[sdk]` extra."""
    from crewd.executor import SdkAttemptExecutor
    import crewd.executor as executor_mod

    # Force the "SDK unavailable" branch deterministically (no real SDK needed).
    monkeypatch.setattr(
        "crewd.sdk_adapter.sdk_available", lambda: False, raising=True
    )
    errs = SdkAttemptExecutor(ops_factory=None).doctor()
    assert len(errs) == 1
    msg = errs[0]
    assert "github-copilot-sdk" in msg
    # Retired remediation must be gone.
    assert "crewd[sdk]" not in msg
    # Must point at reinstalling/upgrading the normal distribution.
    assert "crewd" in msg
    assert "doctor" in msg
    # Repair commands must actually reinstall dependencies of an already-present
    # install (a plain `uv tool install crewd` is a no-op when crewd is already
    # installed — see #26 Advisory). Any recommended repair must use an
    # effective flag.
    assert "--force-reinstall" in msg or "--reinstall" in msg
    if "uv tool install" in msg:
        assert "uv tool install --reinstall" in msg


def test_healthy_when_ops_factory_injected():
    # The selectable/injected path stays SDK-free and healthy.
    from crewd.executor import SdkAttemptExecutor

    assert SdkAttemptExecutor(ops_factory=object).doctor() == []


def test_legacy_backend_migration_has_no_sdk_extra():
    from crewd.backends import LEGACY_COPILOT_MIGRATION, get_backend
    import pytest

    assert "crewd[sdk]" not in LEGACY_COPILOT_MIGRATION
    assert "copilot-sdk" in LEGACY_COPILOT_MIGRATION
    with pytest.raises(ValueError) as ei:
        get_backend("copilot")
    assert "crewd[sdk]" not in str(ei.value)


def test_docs_use_per_subcommand_workspace_flag():
    """Quickstart docs must not use the root-level ``crewd -w <ws> <cmd>`` form.

    ``-w/--workspace`` is defined only on each subcommand (see ``src/crewd/cli.py``),
    so ``crewd -w "$(pwd)" doctor`` fails with ``No such option: -w`` and breaks the
    canonical quickstart for a new operator. The valid form is ``crewd <cmd> ... -w``.
    Guards both operator-facing docs against regressing to the invalid placement.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    # Matches ``crewd -w`` and ``run crewd -w`` (root-level flag before the subcommand).
    bad = re.compile(r"\bcrewd -w\b")
    for name in ("README.md", "SKILL.md"):
        text = (root / name).read_text()
        offenders = [ln for ln in text.splitlines() if bad.search(ln)]
        assert not offenders, (
            f"{name} uses invalid root-level -w placement (must be per-subcommand): "
            f"{offenders}"
        )
