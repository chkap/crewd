"""Fast, deterministic unit regressions for the clean-install harness (#54).

Unlike ``test_clean_install_smoke.py`` (opt-in, builds + installs real
artifacts), these tests stub out the venv/subprocess layer so they run in the
default suite. They lock in two behaviours the live acceptance job depends on:

1. A *relative* ``--dist`` path (as CI passes: ``--dist dist``) must still
   resolve, even though pip is invoked with ``cwd=workroot``. Regression for the
   acceptance failure where pip reported the artifact "file does not exist".
2. Install success is decided by the pip subprocess return code, never by
   scanning output — a successful install whose trailing line is pip's
   "A new release of pip is available" notice must still classify as installed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "clean_install_smoke.py"
    spec = importlib.util.spec_from_file_location("crewd_clean_install_smoke", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _stub_venv(monkeypatch, harness):
    """Replace real venv creation with a cheap bin/ scaffold."""

    class _Builder:
        def __init__(self, **_kwargs):
            pass

        def create(self, directory):
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(harness.venv, "EnvBuilder", _Builder)
    monkeypatch.setattr(harness, "_sha256", lambda _p: "0" * 64)


def test_smoke_one_resolves_relative_artifact_path(monkeypatch, tmp_path):
    harness = _load_harness()
    _stub_venv(monkeypatch, harness)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    artifact_abs = dist_dir / harness.WHEEL_NAME
    artifact_abs.write_bytes(b"fake wheel bytes")
    workroot = tmp_path / "work"
    workroot.mkdir()

    install_targets: list[str] = []

    def fake_run(cmd, *, cwd, env, timeout=180):
        if "install" in cmd:
            install_targets.append(cmd[-1])
        return _FakeResult(returncode=0)

    monkeypatch.setattr(harness, "_run", fake_run)

    # Run from an unrelated cwd and pass a RELATIVE artifact path, mirroring the
    # CI invocation ``--dist dist`` where the harness later runs pip in workroot.
    monkeypatch.chdir(tmp_path)
    relative_artifact = Path("dist") / harness.WHEEL_NAME
    assert not relative_artifact.is_absolute()

    harness.smoke_one(relative_artifact, workroot)

    assert install_targets, "pip install was never invoked"
    resolved = Path(install_targets[0])
    assert resolved.is_absolute(), f"pip got a relative path: {resolved}"
    assert resolved == artifact_abs


@pytest.mark.parametrize(
    "returncode,expected",
    [(0, True), (1, False)],
)
def test_install_classified_by_returncode_despite_pip_notice(
    monkeypatch, tmp_path, returncode, expected
):
    harness = _load_harness()
    _stub_venv(monkeypatch, harness)

    artifact = tmp_path / harness.WHEEL_NAME
    artifact.write_bytes(b"fake wheel bytes")
    workroot = tmp_path / "work"
    workroot.mkdir()

    # A realistic pip run whose LAST line is the upgrade notice, not the
    # "Successfully installed" line. Classification must ignore this and use rc.
    install_output = (
        "Processing ./crewd-0.1.1-py3-none-any.whl\n"
        "Installing collected packages: crewd\n"
        "Successfully installed crewd-0.1.1\n"
        "\n"
        "[notice] A new release of pip is available: 25.0.1 -> 26.2\n"
        "[notice] To update, run: pip install --upgrade pip\n"
    )

    def fake_run(cmd, *, cwd, env, timeout=180):
        if "install" in cmd:
            return _FakeResult(returncode=returncode, stdout=install_output)
        return _FakeResult(returncode=0)

    monkeypatch.setattr(harness, "_run", fake_run)

    result = harness.smoke_one(artifact, workroot)

    assert result["checks"]["install"] is expected
    # Diagnostic detail is retained (bounded) regardless of pass/fail.
    assert "Successfully installed crewd-0.1.1" in result["details"]["install"]
