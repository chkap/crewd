#!/usr/bin/env python
"""Clean-install smoke for the built crewd release artifacts (#52).

Proves that the *shipped* wheel and sdist are self-contained and runnable from a
**clean environment outside the checkout**, with no editable/source-checkout
dependency and without any live SDK or GitHub call. For each artifact it:

1. builds the artifact (or reuses a provided ``--dist`` dir) with ecosystem
   tooling (``uv build --no-sources``);
2. creates a fresh virtual environment in a throwaway temp dir *outside* the
   repository;
3. ``pip install``s the single artifact into that venv (dependencies resolve
   normally from the index — only editable/source-checkout installs are
   forbidden);
4. runs the offline operator surface as real child processes from a temp CWD
   with an isolated ``$HOME`` and ``CREWD_DISABLE_PUBLIC_BUS=1``:

   * ``python -c 'import crewd; print(crewd.__version__)'`` → import + version
     (also asserts the import resolves to site-packages, not the checkout)
   * ``crewd --help`` (lists subcommands)
   * ``crewd init <ws> --repo acme/widget`` (offline scaffold; no clone)
   * ``crewd refresh -w <ws>`` and ``crewd doctor -w <ws>`` on that workspace

It returns a sanitized manifest (per-artifact checks + SHA-256 content
evidence) and self-cleans all temp dirs. Because installing pulls dependencies
from the index and spins up venvs, the pytest wrapper
(``tests/test_clean_install_smoke.py``) is opt-in via ``CREWD_INSTALL_SMOKE=1``;
this harness is import-safe and CI-reusable.

Usage:
    python scripts/clean_install_smoke.py            # build + smoke both artifacts
    python scripts/clean_install_smoke.py --dist DIR # reuse prebuilt artifacts
    python scripts/clean_install_smoke.py --json out.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
WHEEL_NAME = f"crewd-{VERSION}-py3-none-any.whl"
SDIST_NAME = f"crewd-{VERSION}.tar.gz"

REQUIRED_SUBCOMMANDS = ("init", "attach", "doctor", "refresh", "run", "status")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def build_artifacts(out_dir: Path) -> dict:
    """Build wheel + sdist with ``uv build --no-sources`` into *out_dir*."""
    proc = subprocess.run(
        ["uv", "build", "--no-sources", "--out-dir", str(out_dir)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"uv build failed:\n{proc.stdout}\n{proc.stderr}")
    wheel = out_dir / WHEEL_NAME
    sdist = out_dir / SDIST_NAME
    if not wheel.is_file() or not sdist.is_file():
        raise RuntimeError(f"expected artifacts missing in {out_dir}")
    return {"wheel": wheel, "sdist": sdist}


def _run(cmd: list[str], *, cwd: Path, env: dict, timeout: int = 180):
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout
    )


def smoke_one(artifact: Path, workroot: Path) -> dict:
    """Install *artifact* into a fresh venv and exercise the offline surface."""
    venv_dir = workroot / "venv"
    home_dir = workroot / "home"
    cwd_dir = workroot / "cwd"
    for d in (home_dir, cwd_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1. Fresh, isolated virtual environment outside the checkout.
    venv.EnvBuilder(with_pip=True, clear=True).create(str(venv_dir))
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    py = bin_dir / ("python.exe" if os.name == "nt" else "python")
    crewd = bin_dir / ("crewd.exe" if os.name == "nt" else "crewd")

    checks: dict[str, bool] = {}
    details: dict[str, str] = {}

    def record(name: str, ok: bool, info: str = "") -> None:
        checks[name] = bool(ok)
        if info:
            details[name] = info[-800:]

    # 2. Non-editable install of the single artifact (deps resolve from index).
    inst = _run(
        [str(py), "-m", "pip", "install", "--no-cache-dir", str(artifact)],
        cwd=workroot,
        env={**os.environ},
        timeout=900,
    )
    record("install", inst.returncode == 0, inst.stdout + inst.stderr)
    if inst.returncode != 0:
        return {"artifact": artifact.name, "checks": checks, "details": details}

    # Offline, isolated runtime environment for every command.
    cenv = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(home_dir),
        "XDG_CONFIG_HOME": str(home_dir / ".config"),
        "XDG_DATA_HOME": str(home_dir / ".local" / "share"),
        "CREWD_DISABLE_PUBLIC_BUS": "1",
    }

    # 3a. import + authoritative version — proves the package imports cleanly
    #     from site-packages (not the checkout).
    imp = _run(
        [str(py), "-c", "import crewd, sys; sys.stdout.write(crewd.__version__)"],
        cwd=cwd_dir,
        env=cenv,
    )
    record(
        "import_version",
        imp.returncode == 0 and imp.stdout.strip() == VERSION,
        imp.stdout + imp.stderr,
    )
    # Prove we imported from the installed location, never the source checkout.
    loc = _run(
        [str(py), "-c", "import crewd, sys; sys.stdout.write(crewd.__file__)"],
        cwd=cwd_dir,
        env=cenv,
    )
    from_checkout = str(ROOT) in loc.stdout
    record("import_outside_checkout", loc.returncode == 0 and not from_checkout, loc.stdout)

    # 3b. `crewd --help` lists the operator subcommands
    hlp = _run([str(crewd), "--help"], cwd=cwd_dir, env=cenv)
    help_ok = hlp.returncode == 0 and all(s in hlp.stdout for s in REQUIRED_SUBCOMMANDS)
    record("cli_help", help_ok, hlp.stdout + hlp.stderr)

    # 3d. `crewd init` — offline workspace scaffold (no clone / no network)
    ws = cwd_dir / "ws"
    ini = _run(
        [str(crewd), "init", str(ws), "--repo", "acme/widget"],
        cwd=cwd_dir,
        env=cenv,
    )
    init_ok = ini.returncode == 0 and (ws / "crew.yaml").is_file() and (ws / "GOAL.md").is_file()
    record("cli_init", init_ok, ini.stdout + ini.stderr)

    # 3e. `crewd refresh` — re-render agents from crew.yaml (offline)
    ref = _run([str(crewd), "refresh", "-w", str(ws)], cwd=cwd_dir, env=cenv)
    record("cli_refresh", ref.returncode == 0, ref.stdout + ref.stderr)

    # 3f. `crewd doctor` — offline diagnostics on the scaffolded workspace.
    #     A missing target clone is only a warning; doctor must still run.
    doc = _run([str(crewd), "doctor", "-w", str(ws)], cwd=cwd_dir, env=cenv)
    record("cli_doctor", doc.returncode in (0, 1), doc.stdout + doc.stderr)

    return {
        "artifact": artifact.name,
        "sha256": _sha256(artifact),
        "checks": checks,
        "details": details,
    }


def full_smoke(dist_dir: Path | None = None, out_path: Path | None = None) -> dict:
    """Build (unless *dist_dir* given) and smoke both artifacts. Self-cleaning."""
    tmp = Path(tempfile.mkdtemp(prefix="crewd-install-smoke-"))
    manifest: dict = {"version": VERSION, "results": [], "passed": False}
    try:
        if dist_dir is None:
            built = build_artifacts(tmp / "dist")
        else:
            built = {
                "wheel": dist_dir / WHEEL_NAME,
                "sdist": dist_dir / SDIST_NAME,
            }
        for kind in ("wheel", "sdist"):
            artifact = built[kind]
            workroot = tmp / f"work-{kind}"
            workroot.mkdir(parents=True, exist_ok=True)
            res = smoke_one(artifact, workroot)
            res["kind"] = kind
            manifest["results"].append(res)
        manifest["passed"] = all(
            all(r["checks"].values()) and r["checks"] for r in manifest["results"]
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        manifest["cleaned_up"] = not tmp.exists()

    if out_path is not None:
        out_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dist", type=Path, default=None, help="reuse prebuilt artifacts dir")
    ap.add_argument("--json", type=Path, default=None, help="write manifest JSON here")
    args = ap.parse_args(argv)

    manifest = full_smoke(dist_dir=args.dist, out_path=args.json)
    for res in manifest["results"]:
        unmet = [k for k, v in res["checks"].items() if not v]
        status = "PASS" if res["checks"] and not unmet else "FAIL"
        print(f"[{status}] {res['kind']:5} {res.get('artifact', '?')}")
        if res.get("sha256"):
            print(f"        sha256={res['sha256']}")
        if unmet:
            print(f"        unmet: {unmet}")
            for name in unmet:
                snippet = res.get("details", {}).get(name, "").strip().splitlines()
                if snippet:
                    print(f"          {name}: {snippet[-1][:200]}")
    print(f"\noverall: {'PASS' if manifest['passed'] else 'FAIL'}")
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
