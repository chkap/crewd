"""Static contract for the build-once CI workflow (#54).

These offline checks parse ``.github/workflows/ci.yml`` and lock the security
and build-once guarantees so the release-artifact CI cannot silently regress:

* least-privilege, read-only ``GITHUB_TOKEN`` and no OIDC/publish/token path
  in ``ci.yml`` itself (Trusted Publishing lives in the separate ``publish.yml``
  slice, covered by ``tests/test_publish_workflow.py``);
* every third-party action pinned to an immutable 40-hex commit SHA;
* safe ``pull_request``/``push`` triggers and concurrency cancellation;
* the wheel/sdist are built exactly once and later jobs *download* rather than
  rebuild them;
* the Python matrix matches ``Requires-Python`` and the declared Trove
  classifiers (no over/under-claim).

The suite stays offline and never launches the workflow.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"

EXPECTED_MINORS = ["3.11", "3.12", "3.13", "3.14"]


def _load_ci() -> dict:
    with open(CI) as f:
        return yaml.safe_load(f)


def _triggers(wf: dict) -> dict:
    # PyYAML parses the bare `on:` key as the boolean True.
    return wf.get("on", wf.get(True, {}))


def _all_uses(wf: dict) -> list[str]:
    uses: list[str] = []
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                uses.append(step["uses"])
    return uses


def test_ci_workflow_exists():
    assert CI.is_file(), "expected .github/workflows/ci.yml"


def test_ci_workflow_has_no_publish_or_oidc_path():
    # The integration CI (`ci.yml`) must never publish or request OIDC — that is
    # exclusively the job of the separate `publish.yml` (tested elsewhere). Scan
    # only effective YAML (drop comment lines) so documentation that *describes*
    # the excluded features doesn't trip the guard.
    effective = "\n".join(
        ln for ln in CI.read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ).lower()
    assert "pypi-publish" not in effective, "ci.yml must not publish"
    assert "id-token" not in effective, "ci.yml must not request OIDC id-token"
    assert "twine upload" not in effective, "ci.yml must not upload"


def test_permissions_are_read_only():
    wf = _load_ci()
    assert wf.get("permissions") == {"contents": "read"}, wf.get("permissions")
    # No job may escalate to write or request extra scopes.
    for name, job in wf["jobs"].items():
        perms = job.get("permissions")
        if perms is not None:
            for scope, level in perms.items():
                assert level == "read", f"job {name} escalates {scope}={level}"
            assert "id-token" not in perms, name


def test_safe_triggers_and_concurrency():
    wf = _load_ci()
    trig = _triggers(wf)
    assert "pull_request" in trig, "must run on pull_request"
    assert "push" in trig, "must run on push"
    # No dangerous pull_request_target (which would grant secrets to forks).
    assert "pull_request_target" not in trig
    conc = wf.get("concurrency")
    assert conc and conc.get("cancel-in-progress") is True, conc


def test_all_third_party_actions_are_sha_pinned():
    sha = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    for use in _all_uses(_load_ci()):
        assert sha.match(use), f"action not pinned to a 40-hex commit SHA: {use}"


def test_build_once_then_download_not_rebuild():
    wf = _load_ci()
    jobs = wf["jobs"]
    assert {"test", "build", "acceptance"} <= set(jobs), jobs.keys()

    def step_uses(job):
        return [s.get("uses", "") for s in jobs[job].get("steps", [])]

    def step_runs(job):
        return "\n".join(s.get("run", "") for s in jobs[job].get("steps", []))

    # Exactly one build invocation, and only in the `build` job.
    assert "uv build" in step_runs("build")
    assert "uv build" not in step_runs("acceptance")
    assert "uv build" not in step_runs("test")

    # `build` uploads immutable artifacts; `acceptance` depends on it and
    # downloads them instead of rebuilding.
    assert any("upload-artifact" in u for u in step_uses("build"))
    assert jobs["acceptance"].get("needs") == "build"
    assert any("download-artifact" in u for u in step_uses("acceptance"))
    # Acceptance re-verifies the checksums and reuses the #52 harness by --dist.
    accept_run = step_runs("acceptance")
    assert "sha256sum -c" in accept_run
    assert "clean_install_smoke.py --dist" in accept_run


def test_matrix_matches_requires_python_and_classifiers():
    wf = _load_ci()
    for job in ("test", "acceptance"):
        versions = wf["jobs"][job]["strategy"]["matrix"]["python-version"]
        assert versions == EXPECTED_MINORS, f"{job} matrix = {versions}"

    with open(ROOT / "pyproject.toml", "rb") as f:
        proj = tomllib.load(f)["project"]
    assert proj["requires-python"] == ">=3.11"
    classifier_minors = {
        m.group(1)
        for c in proj["classifiers"]
        if (m := re.fullmatch(r"Programming Language :: Python :: (3\.\d+)", c))
    }
    assert classifier_minors == set(EXPECTED_MINORS), classifier_minors


def test_offline_by_default_env():
    wf = _load_ci()
    assert wf.get("env", {}).get("CREWD_DISABLE_PUBLIC_BUS") == "1"
    # The live SDK smoke (Premium/network) must never be enabled in CI.
    assert "CREWD_LIVE_SMOKE" not in CI.read_text()
