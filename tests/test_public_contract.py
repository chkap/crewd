"""Public package contract for crewd 0.1.0 (#44).

Locks the release-facing package contract so it cannot silently drift:

* one authoritative version source shared by ``pyproject.toml`` and
  ``crewd.__version__``;
* MIT licensing declared as metadata *and* shipped as a ``LICENSE`` file;
* complete, standards-based metadata (description, authors, project URLs,
  keywords, classifiers, Python constraint); and
* the decided typed-package policy (crewd is a CLI application, not a typed
  importable library, so no ``py.typed`` marker and no ``Typing :: Typed``
  claim).

These are deterministic, offline checks — no build, network, or SDK required.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import crewd

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"

# Python minors this repo can actually exercise / claims support for. Add a
# minor here only once the build-once CI matrix proves it green (see
# .github/workflows/ci.yml + tests/test_ci_workflow.py); classifiers must not
# overclaim. #54 added 3.12–3.14 after matrix acceptance passed on each.
SUPPORTED_MINORS = {11, 12, 13, 14}


def _pyproject() -> dict:
    with open(PYPROJECT, "rb") as f:
        return tomllib.load(f)


def _project() -> dict:
    return _pyproject()["project"]


# --- one authoritative version source ---------------------------------------

def test_version_is_dynamic_and_sourced_from_package():
    proj = _project()
    assert "version" in proj.get("dynamic", []), (
        "project.version must be dynamic so metadata cannot drift from "
        "crewd.__version__"
    )
    assert "version" not in proj, (
        "a static project.version would create a second version source"
    )
    hatch_version = _pyproject()["tool"]["hatch"]["version"]
    assert hatch_version.get("path") == "src/crewd/__init__.py", (
        "the authoritative version must be read from src/crewd/__init__.py"
    )


def test_runtime_version_matches_distribution_metadata():
    assert re.fullmatch(r"\d+\.\d+\.\d+", crewd.__version__), crewd.__version__
    try:
        from importlib.metadata import version
        dist_version = version("crewd")
    except Exception:
        return
    assert dist_version == crewd.__version__, (
        f"distribution metadata {dist_version!r} drifted from "
        f"crewd.__version__ {crewd.__version__!r}"
    )


# --- MIT licensing -----------------------------------------------------------

def test_license_declared_as_mit_spdx():
    proj = _project()
    assert proj.get("license") == "MIT", proj.get("license")
    assert "LICENSE" in proj.get("license-files", []), (
        "the LICENSE file must be declared so it is packaged"
    )


def test_license_file_present_and_is_mit():
    text = (ROOT / "LICENSE").read_text()
    assert "MIT License" in text
    assert "Permission is hereby granted" in text
    assert "WITHOUT WARRANTY OF ANY KIND" in text


def test_no_license_classifier_alongside_spdx_expression():
    # PEP 639: a SPDX license expression and a License :: classifier must not
    # both be present.
    for c in _project().get("classifiers", []):
        assert not c.startswith("License ::"), (
            f"remove deprecated license classifier {c!r}; SPDX license = 'MIT' "
            "is authoritative"
        )


def test_public_release_wording_replaces_internal_unreleased():
    readme = (ROOT / "README.md").read_text()
    assert "internal / unreleased" not in readme.lower()
    assert "MIT" in readme


# --- complete, truthful metadata --------------------------------------------

def test_core_descriptive_metadata_present():
    proj = _project()
    assert proj.get("description", "").strip()
    assert proj.get("readme") == "README.md"
    assert proj.get("requires-python") == ">=3.11"
    assert proj.get("authors"), "authors must be declared"
    assert proj.get("keywords"), "keywords must be declared"
    assert proj.get("classifiers"), "classifiers must be declared"


def test_project_urls_point_at_the_public_repository():
    urls = _project().get("urls", {})
    for key in ("Repository", "Issues"):
        assert key in urls, f"project.urls must include {key}"
        assert urls[key].startswith("https://github.com/chkap/crewd")


def test_no_unverified_maturity_or_typed_claims():
    classifiers = _project().get("classifiers", [])
    assert not any(c.startswith("Development Status ::") for c in classifiers)
    # Typed-package policy: crewd is a CLI app, not a typed library.
    assert not any(c.startswith("Typing ::") for c in classifiers)


def test_python_minor_classifiers_are_backed_by_support():
    minors = set()
    for c in _project().get("classifiers", []):
        m = re.fullmatch(r"Programming Language :: Python :: 3\.(\d+)", c)
        if m:
            minors.add(int(m.group(1)))
    assert minors, "at least one concrete Python minor classifier is expected"
    assert minors <= SUPPORTED_MINORS, (
        f"Python classifiers {sorted(minors)} claim minors beyond the "
        f"supported/tested set {sorted(SUPPORTED_MINORS)}"
    )


# --- typed-package policy ----------------------------------------------------

def test_no_py_typed_marker_is_shipped():
    # Decision (#44): the importable API is not a supported typed surface, so no
    # py.typed marker is shipped. Flip this deliberately if a typed API lands.
    assert not (ROOT / "src" / "crewd" / "py.typed").exists(), (
        "shipping py.typed asserts a typed public API; crewd's public contract "
        "is the CLI — update the policy and verify with a type checker first"
    )
