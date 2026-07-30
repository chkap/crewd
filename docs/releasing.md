# Releasing crewd to PyPI

crewd publishes to PyPI through **Trusted Publishing (OIDC)** — there is no
long-lived API token stored anywhere. The entire release path lives in
[`.github/workflows/publish.yml`](../.github/workflows/publish.yml) and is
covered by static security tests in `tests/test_publish_workflow.py`.

## The one intentional trigger

The workflow runs **only** when a GitHub Release is *published*
(`on: release: types: [published]`). It never runs on pull requests, plain
tag/branch pushes, or a schedule, so no fork or PR can reach the OIDC/publish
path.

## Cutting a release

1. **Bump the authoritative version.** Edit `__version__` in
   `src/crewd/__init__.py` (the single source Hatchling reads for package
   metadata) and land it on `main`.
2. **Tag the release commit and publish a Release.** The tag MUST be
   `v<version>` matching that `__version__`:

   ```bash
   git tag -a v0.1.0 -m "crewd 0.1.0"
   git push origin v0.1.0
   gh release create v0.1.0 --title "crewd 0.1.0" --notes-file CHANGELOG.md
   ```

   Publishing the Release starts `publish.yml`.
3. **Approve the deployment.** The publish job targets the protected `pypi`
   GitHub environment; a configured reviewer/branch rule must approve the
   deployment before it runs.

## What the workflow guarantees

- **Provenance first.** Before anything is built or published, the `provenance`
  job asserts the release tag equals `v<__version__>` and that the tag resolves
  to the exact commit being built. A mismatch fails the run early.
- **Build once, reuse everywhere.** The wheel + sdist are built a single time
  (`uv build --no-sources`), checksummed, and uploaded. The acceptance matrix
  (Python 3.11–3.14) and the publish job **download** those exact artifacts and
  never rebuild — the bytes that pass acceptance are the bytes that ship. This
  reuses the `#52` clean-install smoke harness (`scripts/clean_install_smoke.py`).
- **Publish only after acceptance is green**, from the protected `pypi`
  environment, using the pinned `pypa/gh-action-pypi-publish` OIDC action.
- **Least privilege.** The workflow default is `contents: read`; only the
  `publish` job adds `id-token: write` (the OIDC token) — nothing requests
  `contents: write`/`packages`, and there is no manual `twine upload`.

## Retry behaviour

Releases are safe to retry:

- **Re-run the failed run** (or re-publish the same Release). The artifacts are
  rebuilt identically from the immutable tag, and provenance re-validates the
  tag/version/commit.
- The publish step uses `skip-existing: true`, so if a previous attempt already
  uploaded some files to PyPI, the retry **skips those files** instead of failing
  on a duplicate — no double-publish, no manual cleanup.
- `concurrency` serialises runs per release tag and **never cancels an
  in-flight publish**, so a superseding event cannot interrupt an upload
  mid-flight.

## One-time operator setup (not performed here)

Before the first real release, a repository operator must, out of band:

- create the protected `pypi` GitHub environment (with the desired reviewers),
  and
- register crewd's PyPI **Trusted Publisher** bound to this repository,
  the `publish.yml` workflow, and the `pypi` environment.

This document and the workflow do not create tags, publish, or request that
setup; they only define the release path.
