"""Static security contract for the Trusted Publishing workflow (#56).

These offline checks parse ``.github/workflows/publish.yml`` and lock every
security guarantee the release path depends on, so it cannot silently regress:

* ONE intentional trigger — a *published* GitHub Release — and no pull-request
  (and therefore no fork/OIDC) path;
* least privilege: read-only default token, and ``id-token: write`` present on
  *only* the ``publish`` job (no ``contents: write``/``packages`` anywhere);
* provenance is validated before build/publish (tag <-> version <-> commit);
* the wheel/sdist are built exactly once and every later job — including
  publish — *downloads* rather than rebuilds them;
* publishing uses the protected ``pypi`` environment and the pinned
  ``pypa/gh-action-pypi-publish`` OIDC action, with idempotent-retry
  (``skip-existing``) and no long-lived token / ``twine upload`` / secret echo;
* every third-party action is pinned to an immutable 40-hex commit SHA;
* concurrency serialises per-tag and never cancels an in-flight publish.

The suite stays offline and never launches the workflow.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS / "publish.yml"

EXPECTED_MINORS = ["3.11", "3.12", "3.13", "3.14"]


def _load() -> dict:
    with open(PUBLISH) as f:
        return yaml.safe_load(f)


def _triggers(wf: dict) -> dict:
    # PyYAML parses the bare `on:` key as the boolean True.
    return wf.get("on", wf.get(True, {}))


def _job_uses(job: dict) -> list[str]:
    return [s["uses"] for s in job.get("steps", []) if "uses" in s]


def _job_runs(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in job.get("steps", []))


def test_publish_workflow_exists():
    assert PUBLISH.is_file(), "expected .github/workflows/publish.yml"


def test_single_intentional_release_trigger_only():
    wf = _load()
    trig = _triggers(wf)
    # Exactly one trigger: a published Release. No push/tag-push, no schedule,
    # and crucially no pull_request(_target) OIDC path.
    assert set(trig) == {"release"}, f"unexpected triggers: {set(trig)}"
    assert trig["release"]["types"] == ["published"], trig["release"]
    for forbidden in ("pull_request", "pull_request_target", "push", "schedule"):
        assert forbidden not in trig, f"forbidden trigger present: {forbidden}"


def test_default_permissions_read_only():
    wf = _load()
    assert wf.get("permissions") == {"contents": "read"}, wf.get("permissions")


def test_only_publish_job_has_id_token_write():
    wf = _load()
    jobs = wf["jobs"]
    for name, job in jobs.items():
        perms = job.get("permissions")
        if name == "publish":
            assert perms == {"id-token": "write"}, perms
        else:
            # No other job may request OIDC or any write scope.
            if perms is not None:
                assert "id-token" not in perms, f"{name} must not request id-token"
                for scope, level in perms.items():
                    assert level == "read", f"{name} escalates {scope}={level}"


def test_no_write_scopes_anywhere():
    text = PUBLISH.read_text()
    effective = "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )
    # The only write scope permitted in the whole file is `id-token: write`
    # (the publish job's OIDC token). No contents/packages/etc. write.
    for m in re.finditer(r"(\w[\w-]*)\s*:\s*write", effective):
        assert m.group(1) == "id-token", f"unexpected write scope: {m.group(0)}"


def test_provenance_validated_before_build_and_publish():
    wf = _load()
    jobs = wf["jobs"]
    assert "provenance" in jobs, jobs.keys()
    # Build and publish both depend (transitively) on provenance.
    assert jobs["build"].get("needs") == "provenance"
    assert "provenance" in jobs["publish"]["needs"]
    runs = _job_runs(jobs["provenance"])
    # Asserts tag <-> authoritative version and tag <-> commit consistency.
    assert "src/crewd/__init__.py" in runs, "provenance must read authoritative version"
    assert 'v${version}' in runs or "v${version}" in runs
    assert "rev-list" in runs, "provenance must confirm the tag's commit"


def test_build_once_then_download_not_rebuild():
    wf = _load()
    jobs = wf["jobs"]
    assert {"build", "acceptance", "publish"} <= set(jobs), jobs.keys()

    # Exactly one build invocation, only in `build`.
    assert "uv build" in _job_runs(jobs["build"])
    for other in ("acceptance", "publish", "provenance"):
        assert "uv build" not in _job_runs(jobs[other]), f"{other} rebuilds"

    # `build` uploads immutable artifacts; downstream jobs download them.
    assert any("upload-artifact" in u for u in _job_uses(jobs["build"]))
    for consumer in ("acceptance", "publish"):
        assert any(
            "download-artifact" in u for u in _job_uses(jobs[consumer])
        ), f"{consumer} must download artifacts"


def test_publish_waits_for_acceptance():
    wf = _load()
    needs = wf["jobs"]["publish"]["needs"]
    assert "acceptance" in needs, f"publish must need acceptance: {needs}"


def test_acceptance_matrix_and_smoke_reuse():
    wf = _load()
    accept = wf["jobs"]["acceptance"]
    versions = accept["strategy"]["matrix"]["python-version"]
    assert versions == EXPECTED_MINORS, versions
    runs = _job_runs(accept)
    assert "sha256sum -c" in runs, "acceptance must re-verify checksums"
    assert "clean_install_smoke.py --dist" in runs, "acceptance must reuse #52 harness"
    assert "twine check" in runs, "acceptance must run twine check"


def test_publish_uses_protected_pypi_environment():
    wf = _load()
    env = wf["jobs"]["publish"].get("environment")
    assert isinstance(env, dict) and env.get("name") == "pypi", env


def test_publish_uses_pinned_pypa_oidc_action_with_safe_retry():
    wf = _load()
    publish = wf["jobs"]["publish"]
    pypa = [
        s for s in publish["steps"]
        if s.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
    ]
    assert len(pypa) == 1, "expected exactly one pypa/gh-action-pypi-publish step"
    step = pypa[0]
    # Pinned to a 40-hex commit SHA.
    assert re.fullmatch(r"pypa/gh-action-pypi-publish@[0-9a-f]{40}", step["uses"]), step["uses"]
    # Idempotent retries: skip files PyPI already has.
    assert step.get("with", {}).get("skip-existing") is True, step.get("with")


def test_no_long_lived_token_or_manual_upload():
    effective = "\n".join(
        ln for ln in PUBLISH.read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ).lower()
    # OIDC only — no username/password/API-token secret auth, no manual upload,
    # and no secret echoing.
    assert "twine upload" not in effective, "must not manually upload"
    assert "pypi_api_token" not in effective and "pypi-api-token" not in effective
    assert "password:" not in effective, "must not use password auth"
    assert "secrets." not in effective, "must not reference long-lived secrets"


def test_all_third_party_actions_sha_pinned():
    wf = _load()
    sha = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    for job in wf["jobs"].values():
        for use in _job_uses(job):
            assert sha.match(use), f"action not pinned to a 40-hex SHA: {use}"


def test_concurrency_never_cancels_in_flight_publish():
    wf = _load()
    conc = wf.get("concurrency")
    assert conc, "publish must define concurrency"
    assert conc.get("cancel-in-progress") is False, conc
    assert "tag_name" in conc.get("group", ""), conc


def test_offline_default_env_for_build_and_accept():
    wf = _load()
    assert wf.get("env", {}).get("CREWD_DISABLE_PUBLIC_BUS") == "1"
    assert "CREWD_LIVE_SMOKE" not in PUBLISH.read_text()
