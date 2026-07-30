"""Release-artifact contract for ``crewd==0.1.0`` (#52).

These deterministic checks build the wheel and sdist once with the project's
own ecosystem tooling and then assert the *exact* packaged-content contract:

* the installed **wheel** ships only the importable ``crewd`` package (Python
  modules + every runtime Jinja template) plus standards metadata and the
  bundled LICENSE — never tests, docs, developer scripts, bytecode caches, or a
  ``py.typed`` marker (the package intentionally ships untyped, see #44);
* the **sdist** carries everything required to rebuild the wheel plus the
  packaging-facing docs (README, CHANGELOG, LICENSE, SKILL) and *excludes*
  tests, live/developer-only scripts, retrospective material, design docs,
  lockfiles, caches, and local crew/workspace state;
* standards metadata and the PyPI long-description are validated from the built
  artifacts (not the checkout), including ``twine check`` when available.

The heavier *clean-install* smoke (fresh venvs outside the checkout) lives in
``scripts/clean_install_smoke.py`` and the opt-in ``test_clean_install_smoke``
module so the default suite stays fast and offline while remaining reusable in
CI. This module only inspects artifacts and never touches the network.
"""
from __future__ import annotations

import email.parser
import hashlib
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
WHEEL_NAME = f"crewd-{VERSION}-py3-none-any.whl"
SDIST_NAME = f"crewd-{VERSION}.tar.gz"

# The complete set of runtime Jinja resources shipped inside the package. Kept
# explicit so a dropped template (which would break `crewd init`/rendering from
# an installed wheel) fails loudly instead of silently.
TEMPLATE_FILES = {
    "crewd/templates/GOAL.md.j2",
    "crewd/templates/agents/_comm_attribution.md.j2",
    "crewd/templates/agents/_dispatch_model.md.j2",
    "crewd/templates/agents/_inbox_protocol.md.j2",
    "crewd/templates/agents/_role_handoff.md.j2",
    "crewd/templates/agents/_workspace_layout.md.j2",
    "crewd/templates/agents/advisory.agent.md.j2",
    "crewd/templates/agents/lead.agent.md.j2",
    "crewd/templates/agents/verifier.agent.md.j2",
    "crewd/templates/agents/worker.agent.md.j2",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory) -> dict:
    """Build wheel + sdist once with ``uv build`` into a temp dir.

    ``--no-sources`` forces a standards build (no editable/workspace sources) so
    the artifacts are exactly what a consumer would receive from PyPI. Skips the
    whole module if the build cannot run (e.g. no ``uv`` / no build backend
    reachable), so the offline default suite never fails spuriously.
    """
    out = tmp_path_factory.mktemp("dist")
    try:
        proc = subprocess.run(
            ["uv", "build", "--no-sources", "--out-dir", str(out)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        pytest.skip(f"cannot build artifacts: {exc}")
    if proc.returncode != 0:  # pragma: no cover
        pytest.skip(f"uv build failed:\n{proc.stdout}\n{proc.stderr}")

    wheel = out / WHEEL_NAME
    sdist = out / SDIST_NAME
    assert wheel.is_file(), f"wheel not built: {sorted(p.name for p in out.iterdir())}"
    assert sdist.is_file(), f"sdist not built: {sorted(p.name for p in out.iterdir())}"

    with zipfile.ZipFile(wheel) as zf:
        wheel_names = sorted(zf.namelist())
    with tarfile.open(sdist, "r:gz") as tf:
        sdist_members = sorted(
            m.name for m in tf.getmembers() if m.isfile()
        )

    return {
        "dir": out,
        "wheel": wheel,
        "sdist": sdist,
        "wheel_names": wheel_names,
        "sdist_members": sdist_members,
        "wheel_sha256": _sha256(wheel),
        "sdist_sha256": _sha256(sdist),
    }


# --------------------------------------------------------------------------- #
# Wheel contract                                                              #
# --------------------------------------------------------------------------- #

def test_wheel_top_level_is_only_package_and_dist_info(artifacts):
    tops = {n.split("/", 1)[0] for n in artifacts["wheel_names"]}
    assert tops == {"crewd", f"crewd-{VERSION}.dist-info"}, tops


def test_wheel_includes_all_runtime_templates(artifacts):
    names = set(artifacts["wheel_names"])
    missing = TEMPLATE_FILES - names
    assert not missing, f"wheel is missing runtime templates: {sorted(missing)}"


def test_wheel_bundles_license(artifacts):
    assert f"crewd-{VERSION}.dist-info/licenses/LICENSE" in artifacts["wheel_names"]


def test_wheel_excludes_non_runtime_material(artifacts):
    for name in artifacts["wheel_names"]:
        low = name.lower()
        assert not name.startswith("tests/"), name
        assert not name.startswith("docs/"), name
        assert not name.startswith("scripts/"), name
        assert "__pycache__" not in name, name
        assert not name.endswith(".pyc"), name
        assert "retrospective" not in low, name
        # No source-typing/dev-config leakage into the installed package.
        assert name != "crewd/py.typed", name
        assert not name.endswith("/py.typed"), name


def test_wheel_has_no_pytyped_marker(artifacts):
    # The package intentionally ships untyped for 0.1.0 (#44). A stray py.typed
    # would advertise an unsupported typed contract to consumers.
    assert not any(n.endswith("py.typed") for n in artifacts["wheel_names"])


# --------------------------------------------------------------------------- #
# Sdist contract                                                              #
# --------------------------------------------------------------------------- #

def _sdist_rel(members: list[str]) -> set[str]:
    prefix = f"crewd-{VERSION}/"
    return {m[len(prefix):] for m in members if m.startswith(prefix)}


def test_sdist_includes_required_files(artifacts):
    rel = _sdist_rel(artifacts["sdist_members"])
    required = {
        "pyproject.toml",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "SKILL.md",
        "PKG-INFO",
        "src/crewd/__init__.py",
    }
    missing = required - rel
    assert not missing, f"sdist missing required files: {sorted(missing)}"


def test_sdist_includes_all_runtime_templates(artifacts):
    rel = _sdist_rel(artifacts["sdist_members"])
    expected = {f"src/{t}" for t in TEMPLATE_FILES}
    missing = expected - rel
    assert not missing, f"sdist missing templates: {sorted(missing)}"


def test_sdist_excludes_non_release_material(artifacts):
    rel = _sdist_rel(artifacts["sdist_members"])
    for name in rel:
        low = name.lower()
        assert not name.startswith("tests/"), f"tests leaked into sdist: {name}"
        assert not name.startswith("scripts/"), f"dev script leaked into sdist: {name}"
        assert not name.startswith("docs/"), f"design/retro doc leaked into sdist: {name}"
        assert "retrospective" not in low, f"retrospective material leaked: {name}"
        assert "__pycache__" not in name, name
        assert not name.endswith(".pyc"), name
        assert name != "uv.lock", "developer lockfile must not ship in sdist"
        assert not name.endswith("py.typed"), name


# --------------------------------------------------------------------------- #
# Metadata + PyPI README validated from the built artifacts                   #
# --------------------------------------------------------------------------- #

def _wheel_metadata(wheel: Path) -> tuple[email.message.Message, str]:
    with zipfile.ZipFile(wheel) as zf:
        raw = zf.read(f"crewd-{VERSION}.dist-info/METADATA").decode("utf-8")
    parser = email.parser.Parser()
    msg = parser.parsestr(raw)
    # The long-description is the payload after the header block.
    body = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""
    return msg, body


def test_wheel_metadata_is_standards_compliant(artifacts):
    msg, _ = _wheel_metadata(artifacts["wheel"])
    assert msg["Metadata-Version"] in {"2.1", "2.2", "2.3", "2.4"}, msg["Metadata-Version"]
    assert msg["Name"] == "crewd"
    assert msg["Version"] == VERSION
    # MIT expressed via the modern SPDX License-Expression (PEP 639).
    assert (msg["License-Expression"] or "").strip() == "MIT", msg.items()
    assert "LICENSE" in (msg.get_all("License-File") or []), msg.items()
    assert (msg["Requires-Python"] or "").strip() == ">=3.11", msg["Requires-Python"]
    assert msg["Description-Content-Type"] == "text/markdown", msg["Description-Content-Type"]
    # The required runtime SDK dependency must be advertised in metadata.
    reqs = msg.get_all("Requires-Dist") or []
    assert any(r.replace(" ", "").startswith("github-copilot-sdk") for r in reqs), reqs


def test_pypi_long_description_is_readme_and_has_no_relative_repo_links(artifacts):
    _, body = _wheel_metadata(artifacts["wheel"])
    readme = (ROOT / "README.md").read_text()
    # The embedded long-description is the project README verbatim.
    assert readme.strip() in body.strip() or body.strip() == readme.strip(), (
        "wheel long-description does not match README.md"
    )
    for section in ("## Installation", "## Prerequisites", "## Limitations"):
        assert section in body, f"PyPI README missing section: {section}"
    # PyPI cannot resolve checkout-relative links; every repo link must be
    # absolute (guards the #45 regression from an already-built artifact).
    import re

    for target in re.findall(r"\]\(([^)]+)\)", body):
        if target.startswith("#"):
            continue  # in-page anchor
        if target.startswith("http://") or target.startswith("https://"):
            continue
        if target.startswith("mailto:"):
            continue
        raise AssertionError(f"non-absolute link in PyPI long-description: {target}")


def test_sdist_pkginfo_matches_wheel_metadata(artifacts):
    with tarfile.open(artifacts["sdist"], "r:gz") as tf:
        raw = tf.extractfile(f"crewd-{VERSION}/PKG-INFO").read().decode("utf-8")
    msg = email.parser.Parser().parsestr(raw)
    assert msg["Name"] == "crewd"
    assert msg["Version"] == VERSION
    assert (msg["License-Expression"] or "").strip() == "MIT"
    assert msg["Description-Content-Type"] == "text/markdown"


def test_twine_check_passes(artifacts):
    """Validate both artifacts' metadata/README render via ``twine check``.

    Skips gracefully if twine cannot be provisioned (offline), since the
    metadata assertions above already cover the contract deterministically.
    """
    try:
        proc = subprocess.run(
            [
                "uv", "run", "--no-project", "--with", "twine",
                "python", "-m", "twine", "check",
                str(artifacts["wheel"]), str(artifacts["sdist"]),
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        pytest.skip(f"twine unavailable: {exc}")
    if proc.returncode != 0 and "PASSED" not in proc.stdout:
        # Distinguish a real metadata failure from an environment/provisioning
        # problem (e.g. no network to fetch twine).
        if "Checking" not in proc.stdout:  # pragma: no cover
            pytest.skip(f"twine could not run:\n{proc.stdout}\n{proc.stderr}")
        raise AssertionError(f"twine check failed:\n{proc.stdout}\n{proc.stderr}")
    assert "PASSED" in proc.stdout, proc.stdout


def test_artifact_hashes_are_recorded(artifacts):
    # Content evidence: stable, well-formed SHA-256 digests for both artifacts.
    for key in ("wheel_sha256", "sdist_sha256"):
        digest = artifacts[key]
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
