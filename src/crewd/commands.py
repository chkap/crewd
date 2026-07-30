"""Command implementations. Each command is a function called from cli.py."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import signal
import sys
import time
import os
from rich.console import Console
from rich.table import Table

from .config import CrewConfig, GoalState, default_config, sha256_file, ROLES
from .workspace import Workspace
from .templates_render import render, write_if_absent
from .backends import get_backend
from . import registry

console = Console()


# ─────────────────────────── init ───────────────────────────
def cmd_init(path: Path, name: str | None, repo: str | None) -> int:
    path = path.resolve()
    ws = Workspace(path)
    if ws.is_initialized():
        console.print(f"[yellow]workspace already initialized:[/] {path}")
        return 1

    path.mkdir(parents=True, exist_ok=True)
    ws.ensure_skeleton()

    name = name or path.name
    cfg = default_config(name=name, remote=repo)
    cfg.save(ws.crew_yaml)

    # GOAL.md
    write_if_absent(ws.goal_md, render("GOAL.md.j2", workspace_name=name))

    # Per-role agent.md
    _render_agent_files(ws, cfg)

    # Register in user-level registry so `crewd list` finds it
    registry.register(name, path, repo)

    console.print(f"[green]✓[/] initialized workspace at [bold]{path}[/]")
    console.print(f"  edit [cyan]{ws.goal_md}[/] then run: [bold]crewd attach <owner/repo>[/] (or pass --repo at init)")
    if repo:
        console.print(f"  attached to [bold]{repo}[/] — run [bold]crewd doctor[/] next")
    return 0


def _render_agent_files(ws: Workspace, cfg: CrewConfig, goal_label: str = "goal:v1") -> None:
    ctx = {
        "workspace_name": cfg.name,
        "target_repo": cfg.target.remote,
        "target_branch": cfg.target.branch,
        "worker_model": cfg.roles["worker"].model if "worker" in cfg.roles else "?",
        "verifier_model": cfg.roles["verifier"].model if "verifier" in cfg.roles else "?",
        "advisory_model": cfg.roles["advisory"].model if "advisory" in cfg.roles else "?",
        "advisory_enabled": "advisory" in cfg.roles,
        "worker_family": cfg.roles["worker"].family if "worker" in cfg.roles else "?",
        "verifier_family": cfg.roles["verifier"].family if "verifier" in cfg.roles else "?",
        "goal_label": goal_label,
    }
    for role in ROLES:
        if role not in cfg.roles:
            continue
        rendered = render(
            f"agents/{role}.agent.md.j2",
            role_model=cfg.roles[role].model,
            role_name=role,
            **ctx,
        )
        # Write AGENTS.md into cfg/<role>/ (Copilot auto-loads from cwd)
        role_dir = ws.role_cfg_dir(role)
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "AGENTS.md").write_text(rendered)


def check_and_render(ws: Workspace, cfg: CrewConfig) -> bool:
    """If crew.yaml is newer than any AGENTS.md, or any are missing, re-render.

    Returns True iff a re-render happened. Logs a one-line notice when it does.
    """
    if not ws.crew_yaml.exists():
        return False
    yaml_mtime = ws.crew_yaml.stat().st_mtime
    needs = False
    for role in ROLES:
        if role not in cfg.roles:
            continue
        amd = ws.role_cfg_dir(role) / "AGENTS.md"
        if not amd.exists() or amd.stat().st_mtime < yaml_mtime:
            needs = True
            break
    if needs:
        _render_agent_files(ws, cfg)
        console.print("[blue]ℹ[/] auto-rendered AGENTS.md from crew.yaml (use --no-auto-render to skip)")
    return needs


# ─────────────────────────── extra_add_dirs advisory ───────────────────────────
def _extra_dir_advisories(ws: Workspace, cfg: CrewConfig) -> list[tuple[str, str]]:
    """Classify configured ``extra_add_dirs`` into (severity, message) advisories.

    Shared by ``doctor`` (issues table), ``refresh`` and run pre-flight so every
    surface reports the same facts about external context (#28) with identical
    wording:

      - a *missing* entry is skipped at run time → ``warn`` (non-fatal);
      - an *external* entry (including a symlink whose canonical target is
        outside the workspace) is a run **blocker** → ``error``. The Copilot SDK
        exposes only a single ``working_directory`` and has no ``--add-dir``
        equivalent, so a path outside the workspace root cannot be mounted; the
        executor refuses to launch a role with such a path. We therefore surface
        it as a non-runnable state *before* any work starts, with secret-safe
        copied/sanitized-context guidance, rather than claiming the canonical
        path is mounted and then failing mid-run.

    Internal entries produce no advisory.
    """
    out: list[tuple[str, str]] = []
    for info in ws.classify_extra_dirs(cfg.extra_add_dirs):
        if info.status == "missing":
            out.append((
                "warn",
                f"extra_add_dirs entry '{info.entry}' does not resolve to a "
                f"directory (looked at {info.canonical}) — it is skipped at run "
                "time. Remove it or fix the path.",
            ))
        elif info.status == "external":
            sym = (
                f" The entry is a symlink whose canonical target ({info.canonical}) "
                "is outside the workspace."
                if info.is_symlink else ""
            )
            out.append((
                "error",
                f"extra_add_dirs entry '{info.entry}' resolves outside the "
                f"workspace to {info.canonical}, which the Copilot SDK cannot "
                f"mount (it has no `--add-dir` equivalent — only a single "
                f"working_directory).{sym} This blocks the run. Copy or sanitize "
                "only the needed context into the workspace and point "
                "extra_add_dirs at that in-workspace path, so secrets outside the "
                "crew are never exposed.",
            ))
    return out


def _external_context_block(ws: Workspace, cfg: CrewConfig) -> list[str]:
    """Return the external-context blocker messages (``error`` advisories) only.

    Used by run pre-flight to refuse a workspace whose ``extra_add_dirs`` point
    outside the workspace (unmountable by the SDK) *before* any backend probe,
    dispatch, attempt reservation, or SDK session is started (#28).
    """
    return [msg for sev, msg in _extra_dir_advisories(ws, cfg) if sev == "error"]


# ─────────────────────────── refresh ───────────────────────────
def cmd_refresh(workspace: Path) -> int:
    """Force re-render agents/*.agent.md + AGENTS.md from templates + crew.yaml.

    Also performs workspace migration if needed:
    - Renames checkout/ → repo/ if the old layout is detected
    - Updates crew.yaml fields (legacy ``checkout``/``repo`` → new
      ``repo``/``remote`` schema) via save() roundtrip
    - Creates per-role git worktrees if missing
    """
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    cfg = CrewConfig.load(ws.crew_yaml)
    cfg_dirty = False

    # ── migrate: retired crew.yaml schema (backend etc.) ──
    # Unknown user keys are preserved across this save (config models allow
    # extras), so the upgrade is non-destructive (#28).
    for note in cfg.apply_migrations():
        console.print(f"[blue]ℹ[/] {note}")
        cfg_dirty = True

    # ── migrate: checkout/ → repo/ ──
    old_co = (ws.root / "checkout").resolve()
    target_co = (ws.root / "repo").resolve()
    if old_co.exists() and old_co != target_co and not target_co.exists():
        old_co.rename(target_co)
        console.print(f"[blue]ℹ[/] migrated checkout/ → repo/")
    if cfg.target.repo == "./checkout":
        cfg.target.repo = "./repo"
        cfg_dirty = True
        console.print(f"[blue]ℹ[/] updated crew.yaml target.repo → ./repo")
    elif (ws.root / "repo").exists() or cfg.target.remote:
        # Touch-save once to canonicalize legacy keys (checkout→repo, repo→remote)
        cfg_dirty = True
    if cfg_dirty:
        cfg.save(ws.crew_yaml)

    # ── create worktrees if repo exists but worktrees don't ──
    repo = ws.repo_dir(cfg.target.repo)
    if repo.exists():
        created = _setup_worktrees(ws, cfg, repo)
        for wt_path in created:
            console.print(f"  [green]✓[/] worktree {wt_path}")

    # ── render templates ──
    goal_label = "goal:v1"
    if ws.goal_json.exists():
        try:
            goal_label = GoalState.load(ws.goal_json).label
        except Exception:
            pass
    _render_agent_files(ws, cfg, goal_label=goal_label)
    for role in ROLES:
        if role in cfg.roles:
            console.print(f"  [green]✓[/] {ws.role_cfg_dir(role) / 'AGENTS.md'}")
    for sev, msg in _extra_dir_advisories(ws, cfg):
        tag = "[yellow]warn[/]" if sev == "warn" else "[red]ERROR[/]"
        console.print(f"  {tag} {msg}")
    console.print(f"[green]✓[/] refreshed")
    return 0


# ─────────────────────────── worktree helpers ───────────────────────────
def _setup_worktrees(ws: Workspace, cfg: CrewConfig, repo_dir: Path) -> list[Path]:
    """Create per-role git worktrees from the main repo clone.

    Returns list of worktree paths that were created (skips existing).
    """
    created: list[Path] = []
    branch = cfg.target.branch
    for role in ROLES:
        if role not in cfg.roles:
            continue
        wt = ws.role_worktree(role)
        if wt.exists():
            continue
        rc = subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(wt), branch],
            capture_output=True, text=True, check=False,
        )
        if rc.returncode == 0:
            created.append(wt)
        else:
            console.print(f"    [yellow]worktree add for {role} failed: {rc.stderr.strip()}[/]")
    return created


# ─────────────────────────── attach ───────────────────────────
def cmd_attach(workspace: Path, repo: str, branch: str | None, clone: bool) -> int:
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    cfg = CrewConfig.load(ws.crew_yaml)
    cfg.target.remote = repo
    if branch:
        cfg.target.branch = branch
    cfg.save(ws.crew_yaml)
    # Use existing goal label if present, else default
    goal_label = "goal:v1"
    if ws.goal_json.exists():
        try:
            goal_label = GoalState.load(ws.goal_json).label
        except Exception:
            pass
    _render_agent_files(ws, cfg, goal_label=goal_label)  # refresh agent.md with repo name
    registry.register(cfg.name, ws.root, repo)  # update registry repo field

    co = ws.repo_dir(cfg.target.repo)
    if clone and not co.exists():
        console.print(f"[blue]cloning[/] {repo} → {co}")
        rc = subprocess.run(
            ["gh", "repo", "clone", repo, str(co), "--", "-b", cfg.target.branch],
            check=False,
        ).returncode
        if rc != 0:
            console.print(f"[yellow]clone failed (rc={rc}); attach config saved anyway[/]")
        else:
            # Create per-role git worktrees from the main clone
            created = _setup_worktrees(ws, cfg, co)
            for wt_path in created:
                console.print(f"  [green]✓[/] worktree {wt_path}")
            # Render AGENTS.md into each worktree
            _render_agent_files(ws, cfg, goal_label=goal_label)
    console.print(f"[green]✓[/] attached to [bold]{repo}[/] @ {cfg.target.branch}")
    return 0


# ─────────────────────────── doctor ───────────────────────────
def _inbox_summary(ws: Workspace, role: str) -> tuple[int, int, int]:
    """Return (pending, delivering, processed) counts for a role's inbox.

    Uses the host-owned :class:`~crewd.inbox.InboxService` so both message
    formats are counted and in-flight (attached-but-unacked) deliveries and the
    processed audit trail are visible — without surfacing message content.
    """
    from .inbox import InboxService

    try:
        c = InboxService.for_workspace(ws).counts(role)
        return c["pending"], c["delivering"], c["processed"]
    except Exception:
        return 0, 0, 0


def _read_goal_cycle(ws: Workspace) -> int | None:
    """Best-effort read of cycle from state/goal.json if present (PR1 forward-compat)."""
    gj = ws.state_dir / "goal.json"
    if not gj.exists():
        return None
    try:
        import json
        data = json.loads(gj.read_text())
        c = data.get("cycle")
        if c is None:
            c = data.get("cycles_used")
        return int(c) if c is not None else None
    except Exception:
        return None


def cmd_doctor(workspace: Path) -> int:
    ws = Workspace(workspace.resolve())
    issues: list[tuple[str, str]] = []  # (severity, message); severity in {"error","warn"}

    console.rule(f"[bold]crewd doctor[/] — {ws.root}")

    if not ws.is_initialized():
        console.print(f"[red]✗ no crew.yaml at[/] {ws.root}")
        return 1

    try:
        cfg = CrewConfig.load(ws.crew_yaml)
    except Exception as e:
        console.print(f"[red]✗ crew.yaml invalid:[/] {e}")
        return 1

    console.print(
        f"[green]✓[/] crew.yaml valid  ([bold]{cfg.name}[/], "
        f"backend=[cyan]{cfg.backend}[/], target=[cyan]{cfg.target.remote or '(unset)'}[/])"
    )

    # Backend migration / SDK readiness (issue #28): distinguish a
    # migration-required legacy workspace from a missing-SDK install from a
    # runnable one. A pending schema migration is actionable via `crewd refresh`;
    # only once the backend is the current SDK do we probe that the SDK imports.
    pending = cfg.pending_migrations()
    if pending:
        for note in pending:
            issues.append(("error", f"migration required — run `crewd refresh` ({note})"))
    else:
        try:
            backend = get_backend(cfg.backend)
        except ValueError as e:
            issues.append(("error", str(e)))
        else:
            for e in backend.doctor():
                issues.append(("error", e))

    # External context declared via extra_add_dirs (issue #28).
    for sev, msg in _extra_dir_advisories(ws, cfg):
        issues.append((sev, msg))

    # Roles table
    roles_tbl = Table(title="roles", show_lines=False)
    roles_tbl.add_column("role"); roles_tbl.add_column("model"); roles_tbl.add_column("family")
    roles_tbl.add_column("AGENTS.md"); roles_tbl.add_column("session-state"); roles_tbl.add_column("last log")
    yaml_mtime = ws.crew_yaml.stat().st_mtime
    logs_root = ws.state_dir / "logs"
    for role in ROLES:
        if role not in cfg.roles:
            continue
        rc = cfg.roles[role]
        amd = ws.role_cfg_dir(role) / "AGENTS.md"
        if amd.exists():
            if amd.stat().st_mtime < yaml_mtime:
                amd_str = "[yellow]stale[/]"
                issues.append(("warn", f"cfg/{role}/AGENTS.md is older than crew.yaml — run `crewd refresh`"))
            else:
                amd_str = "[green]ok[/]"
        else:
            amd_str = "[red]missing[/]"
            issues.append(("error", f"cfg/{role}/AGENTS.md missing — run `crewd refresh`"))
        sdir = ws.role_cfg_dir(role) / "session-state"
        if sdir.exists():
            if not sdir.is_dir():
                sess_str = "[red]corrupt[/]"
                issues.append(("error", f"cfg/{role}/session-state exists but is not a directory"))
            else:
                sess_str = "[green]present[/]"
        else:
            sess_str = "[dim]none[/]"
        ldir = logs_root / role
        current_label = _goal_label_for_logs(ws)
        if current_label:
            ldir = ws.role_logs_dir(role, current_label)
        last_log_str = "[dim]—[/]"
        if ldir.exists():
            logs_ = sorted(ldir.glob("*.log"))
            if logs_:
                ts = datetime.fromtimestamp(logs_[-1].stat().st_mtime, tz=timezone.utc)
                last_log_str = ts.isoformat(timespec="seconds")
        roles_tbl.add_row(role, rc.model, rc.family, amd_str, sess_str, last_log_str)
    console.print(roles_tbl)

    # Family check
    for e in cfg.validate_families():
        issues.append(("error", e))

    # State table
    cycle = ws.read_cycle()
    goal_cycle = _read_goal_cycle(ws)
    src = "cycle.txt"
    if goal_cycle is not None:
        cycle = goal_cycle
        src = "goal.json"
    state_tbl = Table(title="state")
    state_tbl.add_column("field"); state_tbl.add_column("value")
    state_tbl.add_row("STOPPED", "yes" if ws.is_stopped() else "no")
    state_tbl.add_row("PAUSED", ws.pause_reason() or "no")
    # Daemon liveness + stale-PID repair. `status` is read-only, so `doctor` is
    # the maintenance command that actually clears a stale PID file.
    pid = ws.read_pid()
    if pid is not None:
        if ws.is_daemon_alive():
            state_tbl.add_row("daemon", f"PID {pid} (running)")
        else:
            state_tbl.add_row("daemon", f"PID {pid} (dead — cleaning up)")
            ws.clear_pid()
            issues.append(("warn", f"cleared stale daemon PID file (PID {pid} not alive)"))
    else:
        state_tbl.add_row("daemon", "not running")
    state_tbl.add_row(
        "cycle",
        f"{cycle} / {cfg.loop.max_cycles or '∞'}  (from {src})",
    )
    if ws.is_stopped() and cycle == 0:
        issues.append(("error", "STOPPED present at cycle 0 — workspace is stuck. Run `crewd resume` then `crewd run`."))
    if cfg.loop.max_cycles and cycle >= cfg.loop.max_cycles:
        issues.append(("warn", f"cycle ({cycle}) has reached max_cycles ({cfg.loop.max_cycles})"))
    console.print(state_tbl)

    # Inbox table
    in_tbl = Table(title="inbox")
    in_tbl.add_column("role"); in_tbl.add_column("pending"); in_tbl.add_column("delivering"); in_tbl.add_column("processed")
    for role in ROLES:
        if role not in cfg.roles:
            continue
        pending, delivering, processed = _inbox_summary(ws, role)
        in_tbl.add_row(role, str(pending), str(delivering), str(processed))
    console.print(in_tbl)

    # Public-write durability table (issue #29): reserved-but-unverified intents
    # are recoverable side effects a `crewd run` reconciles; surface them so an
    # operator can see a stuck/offline write without reading the journal by hand.
    try:
        from .public_writer import IntentStore

        store = IntentStore.for_workspace(ws)
        counts = store.counts()
        if counts["pending"] or counts["verified"]:
            pw_tbl = Table(title="public writes")
            pw_tbl.add_column("state"); pw_tbl.add_column("count")
            pw_tbl.add_row("verified", str(counts["verified"]))
            pw_tbl.add_row("reserved (unverified)", str(counts["pending"]))
            console.print(pw_tbl)
            if counts["pending"]:
                issues.append((
                    "warn",
                    f"{counts['pending']} public write(s) reserved but unverified — "
                    "`crewd run` reconciles them once GitHub is reachable.",
                ))
    except Exception:
        pass

    # Recent activity
    logs_root = ws.state_dir / "logs"
    recent: list[tuple[Path, float]] = []
    if logs_root.exists():
        for p in logs_root.rglob("*.log"):
            recent.append((p, p.stat().st_mtime))
    recent.sort(key=lambda x: x[1], reverse=True)
    act_tbl = Table(title="recent activity (last 3 cycle logs)")
    act_tbl.add_column("when"); act_tbl.add_column("path")
    if recent:
        for p, mt in recent[:3]:
            act_tbl.add_row(datetime.fromtimestamp(mt, tz=timezone.utc).isoformat(timespec="seconds"), str(p))
    else:
        act_tbl.add_row("—", "(no logs yet)")
    console.print(act_tbl)

    # Soft warnings
    if not cfg.target.remote:
        issues.append(("warn", "no target repo attached. Run: crewd attach <owner/repo>"))
    else:
        co = ws.repo_dir(cfg.target.repo)
        if not co.exists():
            issues.append(("warn", f"target repo clone missing at {co}. Run: crewd attach {cfg.target.remote} --clone"))

    if not ws.goal_md.exists():
        issues.append(("error", "GOAL.md missing."))
    else:
        body = ws.goal_md.read_text(errors="replace")
        if "Replace this file with your concrete goal" in body:
            issues.append(("warn", "GOAL.md still has template placeholder. Edit it before running."))

    if issues:
        console.print("\n[bold]suggestions:[/]")
        for sev, msg in issues:
            tag = "[red]ERROR[/]" if sev == "error" else "[yellow]warn [/]"
            console.print(f"  {tag} {msg}")
    else:
        console.print("\n[green]✓ no issues[/]")

    return 1 if any(s == "error" for s, _ in issues) else 0


# ─────────────────────────── goal ───────────────────────────
def cmd_goal(workspace: Path, edit: bool, *, from_path: Path | None = None) -> int:
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    if from_path is not None:
        src = from_path if from_path.is_absolute() else (Path.cwd() / from_path)
        if not src.exists():
            console.print(f"[red]source GOAL file not found:[/] {src}")
            return 1
        if src.resolve() != ws.goal_md.resolve():
            ws.goal_md.write_text(src.read_text())
        console.print(f"[green]✓[/] GOAL.md installed from {src}")
        return 0
    if edit:
        editor = subprocess.os.environ.get("EDITOR", "vi")
        subprocess.run([editor, str(ws.goal_md)])
        return 0
    console.print(ws.goal_md.read_text())
    return 0


# ─────────────────────────── run ───────────────────────────
def _load_or_init_goal_state(ws: Workspace) -> GoalState:
    """Load goal.json, or create v1 from existing GOAL.md / cycle.txt for back-compat."""
    if ws.goal_json.exists():
        return GoalState.load(ws.goal_json)
    sha = sha256_file(ws.goal_md) if ws.goal_md.exists() else ""
    cycles = 0
    if ws.cycle_file.exists():
        try:
            cycles = int(ws.cycle_file.read_text().strip() or "0")
        except ValueError:
            cycles = 0
    state = GoalState(version=1, goal_md_sha256=sha, label="goal:v1", cycles=cycles)
    state.save(ws.goal_json)
    return state


def _write_exit_reason(ws: Workspace, reason: str) -> None:
    ws.state_dir.mkdir(parents=True, exist_ok=True)
    ws.exit_reason_file.write_text(reason + "\n")
    console.print(f"[blue]exit-reason:[/] {reason}")


def _goal_label_for_logs(ws: Workspace) -> str | None:
    if not ws.goal_json.exists():
        return None
    try:
        return GoalState.load(ws.goal_json).label
    except Exception:
        return None


def build_executor(cfg: CrewConfig):
    """Construct the typed AttemptExecutor the orchestrator drives.

    Kept as a module-level seam so tests can inject a deterministic fake without
    the SDK. Only the SDK transport is supported now that the legacy subprocess
    backend is retired; ``_preflight`` rejects any other ``backend`` value before
    this is reached.
    """
    from .executor import SdkAttemptExecutor

    return SdkAttemptExecutor()


def _preflight(workspace: Path, auto_render: bool) -> tuple[Workspace, CrewConfig, object, GoalState] | int:
    """Shared pre-flight checks for foreground and daemon run modes.

    Returns (ws, cfg, backend, goal_state) on success, or an int exit code on failure.
    """
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    cfg = CrewConfig.load(ws.crew_yaml)

    if auto_render:
        check_and_render(ws, cfg)

    fam_errs = cfg.validate_families()
    if fam_errs:
        for e in fam_errs:
            console.print(f"[red]family check:[/] {e}")
        return 2

    # Backend / migration gate (issue #28): a legacy `backend: copilot` workspace
    # is migration-required — refuse before any work with the exact action rather
    # than a generic unknown-backend error.
    if cfg.pending_migrations():
        console.print(
            "[red]backend: migration required[/] — this workspace still uses the "
            "retired `backend: copilot` subprocess transport. Run "
            "[bold]crewd refresh[/] to migrate to `copilot-sdk`, then "
            "[bold]crewd doctor[/]."
        )
        return 2

    # External context gate (issue #28): unsupported external `extra_add_dirs`
    # (paths — including symlink targets — outside the workspace root) are a
    # distinct non-runnable state because the Copilot SDK cannot mount them. This
    # is validated *before* backend selection and `backend.doctor()` so it is
    # never masked by (or reordered behind) a missing-SDK error, and refused
    # before any dispatch, attempt reservation, handoff consumption, or SDK
    # session — with secret-safe copied/sanitized-context guidance — so authority
    # never advances on a workspace that cannot actually run. Missing entries stay
    # non-fatal (skipped at run time).
    ext_block = _external_context_block(ws, cfg)
    if ext_block:
        for msg in ext_block:
            console.print(f"[red]extra_add_dirs:[/] {msg}")
        return 2

    try:
        backend = get_backend(cfg.backend)
    except ValueError as e:
        # Unknown backend: refuse before any work with a diagnostic.
        console.print(f"[red]backend:[/] {e}")
        return 2
    bd_errs = backend.doctor()
    if bd_errs:
        for e in bd_errs:
            console.print(f"[red]backend:[/] {e}")
        return 2

    # Non-fatal extra_add_dirs advisories (e.g. a missing/stale entry that is
    # simply skipped at run time). Any external-context blocker already returned
    # above, so only warnings remain here.
    for _sev, msg in _extra_dir_advisories(ws, cfg):
        console.print(f"[yellow]extra_add_dirs:[/] {msg}")

    co = ws.repo_dir(cfg.target.repo)
    if not co.exists():
        console.print(f"[red]target repo clone missing:[/] {co}. Run [bold]crewd attach --clone[/].")
        return 2

    goal_state = _load_or_init_goal_state(ws)

    if ws.goal_md.exists() and goal_state.goal_md_sha256:
        current_sha = sha256_file(ws.goal_md)
        if current_sha != goal_state.goal_md_sha256:
            console.print(
                f"[red]GOAL.md changed since goal v{goal_state.version} started;[/] "
                f"run [bold]crewd new-goal --from GOAL.md[/] to start a new epoch"
            )
            return 2

    return ws, cfg, backend, goal_state


def _make_github_client(cfg: CrewConfig):
    """Production GitHub effect seam for the public bus (patchable in tests).

    Returns a :class:`~crewd.github_bus.CliGitHubClient` bound to the target
    remote, or ``None`` when no remote is configured (nothing to validate).
    """
    remote = cfg.target.remote
    if not remote:
        return None
    from .github_bus import CliGitHubClient

    return CliGitHubClient(remote)


def _build_bus_gate(cfg: CrewConfig, goal_state: GoalState):
    """Construct the default-on public-bus gate for a normal run.

    Wires ``CliGitHubClient`` → ``PublicBus`` → ``PublicBusGate`` so a plain
    ``crewd run`` enforces the public issue-bus invariant (issue #29): Lead cannot
    route Worker/Verifier or finish the goal until the required GitHub artifacts
    are verified. The active task and final-acceptance references are derived from
    the public record, not hard-coded. Returns ``None`` (inert) only when the
    workspace is not yet attached to a remote or an operator explicitly disables
    the bus via ``CREWD_DISABLE_PUBLIC_BUS`` (e.g. offline recovery).
    """
    if os.environ.get("CREWD_DISABLE_PUBLIC_BUS"):
        return None
    client = _make_github_client(cfg)
    if client is None:
        return None
    from .github_bus import PublicBus, PublicBusGate

    bus = PublicBus(
        client,
        crew=cfg.name,
        expected_repo=cfg.target.remote,
        goal_label=goal_state.label or "goal:v1",
    )
    return PublicBusGate(bus)


def _build_publisher(ws: Workspace, cfg: CrewConfig, goal_state: GoalState):
    """Construct the default-on durable public-write publisher for a normal run.

    Wires ``CliGitHubClient`` → ``PublicBus`` → ``PublicWriter`` with a durable
    intent journal under ``state/public_writes`` so a plain ``crewd run`` publishes
    every material role handoff / Lead decision as a verified GitHub artifact
    exactly once, reconciling on restart (issue #29). Inert under the same guard as
    the gate: no remote, or ``CREWD_DISABLE_PUBLIC_BUS`` set.
    """
    if os.environ.get("CREWD_DISABLE_PUBLIC_BUS"):
        return None
    client = _make_github_client(cfg)
    if client is None:
        return None
    from .github_bus import PublicBus
    from .public_writer import IntentStore, PublicWriter

    bus = PublicBus(
        client,
        crew=cfg.name,
        expected_repo=cfg.target.remote,
        goal_label=goal_state.label or "goal:v1",
    )
    return PublicWriter(bus, IntentStore.for_workspace(ws))


def _build_orchestrator(ws: Workspace, cfg: CrewConfig, goal_state: GoalState):
    from .orchestrator import Orchestrator

    executor = build_executor(cfg)
    # Gated, test-only live-smoke seam: default absent → production is untouched.
    # When CREWD_SMOKE_POLICY points at a policy file, a bounded instruction
    # suffix is appended to the production-rendered prompts (the handoff payload
    # rendering itself is never replaced). See crewd._smoke / scripts/live_smoke.py.
    prompt_policy = None
    if os.environ.get("CREWD_SMOKE_POLICY"):
        from ._smoke import SmokePromptPolicy

        prompt_policy = SmokePromptPolicy.from_env()
    bus_gate = _build_bus_gate(cfg, goal_state)
    publisher = _build_publisher(ws, cfg, goal_state)
    return Orchestrator(
        ws, cfg, executor, goal_state, prompt_policy=prompt_policy,
        bus_gate=bus_gate, publisher=publisher,
    )


def cmd_run(workspace: Path, once: bool, role: str | None, auto_render: bool = True) -> int:
    """Foreground dispatcher-driven run.

    Advances the goal run until Lead finishes/waits/pauses, the work budget
    exhausts, or an operator control/signal halts it — each mapped to a distinct
    persisted exit reason. ``--once`` advances exactly one dispatch step.
    ``--role X`` runs a single tick of role X through the SDK backend directly
    (bypassing the dispatcher), for manual/debug use.
    auto_render: if True (default), re-render agents/ when crew.yaml is newer.
    """
    result = _preflight(workspace, auto_render)
    if isinstance(result, int):
        return result
    ws, cfg, backend, goal_state = result

    if role:
        cycle = goal_state.cycles
        return _tick_role(ws, cfg, backend, role, cycle)

    # Clear stale exit-reason at start (a report artifact, not a durable blocker)
    if ws.exit_reason_file.exists():
        ws.exit_reason_file.unlink()
    # NOTE: a plain `crewd run` deliberately does NOT clear STOPPED/PAUSED
    # sentinels or auto-resume a paused/stopped dispatcher run — that would erase
    # a durable human blocker (the bypass fixed in PR #16). Reactivating a
    # halted run is the explicit `crewd resume` workflow's job.
    orch = _build_orchestrator(ws, cfg, goal_state)
    # Install signal handlers (SIGINT, SIGTERM) — graceful stop
    prev_int = signal.signal(signal.SIGINT, orch.request_stop)
    prev_term = signal.signal(signal.SIGTERM, orch.request_stop)
    try:
        return orch.run(once)
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)


def cmd_run_daemon(workspace: Path, once: bool, auto_render: bool = True) -> int:
    """Fork the run loop into a background daemon process.

    Pre-flight checks run in the foreground so errors are visible immediately.
    On success the foreground exits 0 and the daemon PID is printed.
    """
    import os as _os

    result = _preflight(workspace, auto_render)
    if isinstance(result, int):
        return result
    ws, cfg, backend, goal_state = result

    if ws.is_daemon_alive():
        pid = ws.read_pid()
        console.print(f"[red]daemon already running[/] (PID {pid}). Stop it first with [bold]crewd stop[/].")
        return 1

    # Double-fork to fully detach
    pid1 = _os.fork()
    if pid1 > 0:
        # Parent: wait briefly for child to write PID, then report
        time.sleep(0.3)
        daemon_pid = ws.read_pid()
        if daemon_pid and ws.is_daemon_alive():
            console.print(f"[green]✓[/] daemon started (PID {daemon_pid})")
            console.print(f"  log: {ws.daemon_log}")
            console.print(f"  stop: [bold]crewd stop[/]")
        else:
            console.print(f"[green]✓[/] daemon forked (check [bold]crewd status[/] shortly)")
        return 0

    # First child: new session
    _os.setsid()
    pid2 = _os.fork()
    if pid2 > 0:
        _os._exit(0)  # first child exits

    # Second child (daemon): redirect IO, write PID, run loop
    ws.daemon_log.parent.mkdir(parents=True, exist_ok=True)
    log_fd = _os.open(str(ws.daemon_log), _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND, 0o644)
    dev_null = _os.open(_os.devnull, _os.O_RDONLY)
    _os.dup2(dev_null, 0)
    _os.dup2(log_fd, 1)
    _os.dup2(log_fd, 2)
    _os.close(dev_null)
    _os.close(log_fd)

    ws.write_pid(_os.getpid())

    # Clear stale state (exit-reason is a report artifact; do NOT clear the
    # STOPPED/PAUSED sentinels — a plain daemon start must not erase a durable
    # human blocker. Use `crewd resume` to reactivate a halted run.)
    if ws.exit_reason_file.exists():
        ws.exit_reason_file.unlink()

    orch = _build_orchestrator(ws, cfg, goal_state)
    signal.signal(signal.SIGINT, orch.request_stop)
    signal.signal(signal.SIGTERM, orch.request_stop)
    try:
        rc = orch.run(once)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        rc = 1
    finally:
        ws.clear_pid()
    _os._exit(rc)


def _tick_role(ws: Workspace, cfg: CrewConfig, backend, role: str, cycle: int) -> int:
    if role not in cfg.roles:
        console.print(f"[red]unknown role:[/] {role}")
        return 1
    role_cfg = cfg.roles[role]
    cfg_dir = ws.role_cfg_dir(role)
    goal_label = _goal_label_for_logs(ws) or "goal:v1"
    log_path = ws.log_file(role, cycle, goal_label)
    first_run = not (cfg_dir / "session-state").exists()

    # cwd = cfg/<role>/ so Copilot auto-loads AGENTS.md from there.
    # The role's git worktree is added via --add-dir for file access.
    cwd = ws.role_cfg_dir(role)
    wt = ws.role_worktree(role)

    # Host-owned operator inbox delivery (issue #29): the debug/manual tick path
    # also attaches operator messages via the host and archives them afterwards,
    # rather than instructing the model to read/clear its own inbox file.
    from .inbox import InboxService

    inbox = InboxService.for_workspace(ws)
    manual_attempt = f"manual-{goal_label}-{cycle}"
    inbox_payload = inbox.deliver(role, manual_attempt)

    base_prompt = (
        f"This is cycle {cycle}. Read the latest GitHub issues + comments in "
        f"`{cfg.target.remote}` and execute your role's responsibilities. "
        f"GOAL.md is at `{ws.goal_md}`. "
        f"Any pending operator messages are delivered inline by the host at the "
        f"top of this prompt under an `OPERATOR INBOX` banner (honor them first; "
        f"an `[OVERRIDE]` takes precedence over GOAL.md). "
        f"Do one tick and stop."
    )
    prompt = f"{inbox_payload}\n\n{base_prompt}" if inbox_payload else base_prompt
    add_dirs = [ws.root]  # workspace as readable context
    if wt.exists():
        add_dirs.append(wt)
    # Extra host dirs declared in crew.yaml (deploy checkouts, data dirs, etc.)
    add_dirs += ws.resolve_extra_dirs(cfg.extra_add_dirs)
    console.print(f"  [magenta]{role}[/] ({role_cfg.model}) → {log_path}")
    rc = backend.run_role(
        role=role,
        model=role_cfg.model,
        config_dir=cfg_dir,
        add_dirs=add_dirs,
        prompt=prompt,
        log_path=log_path,
        timeout=role_cfg.per_tick_timeout or cfg.loop.per_tick_timeout,
        cwd=cwd,
        first_run=first_run,
        goal_label=goal_label,
        workspace_root=ws.root,
    )
    if rc != 0:
        console.print(f"    [yellow]{role} exited rc={rc}[/]")
    # The manual tick has completed → archive any delivered operator inbox.
    inbox.acknowledge(role, manual_attempt)
    return rc


# ─────────────────────────── stop / status ───────────────────────────
def cmd_stop(workspace: Path, reason: str, force: bool = False) -> int:
    ws = Workspace(workspace.resolve())
    ws.stop(reason)
    console.print(f"[green]✓[/] STOPPED ({reason})")

    pid = ws.read_pid()
    if pid is not None and ws.is_daemon_alive():
        sig = signal.SIGKILL if force else signal.SIGINT
        sig_name = "SIGKILL" if force else "SIGINT"
        try:
            os.kill(pid, sig)
            console.print(f"[green]✓[/] sent {sig_name} to daemon (PID {pid})")
        except OSError as exc:
            console.print(f"[yellow]warn:[/] could not signal PID {pid}: {exc}")
        if force:
            ws.clear_pid()
    elif pid is not None:
        # Stale PID file
        console.print(f"[blue]ℹ[/] clearing stale PID file (PID {pid} not running)")
        ws.clear_pid()
    return 0


def cmd_pause(workspace: Path, reason: str) -> int:
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    ws.pause(reason)
    console.print(f"[yellow]✓[/] PAUSED ({reason})")
    console.print("[blue]resume after resolving the blocker with `crewd resume`, then `crewd run`[/]")
    return 0


def cmd_status(workspace: Path, as_json: bool = False) -> int:
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    from .diagnostics import build_snapshot

    cfg = CrewConfig.load(ws.crew_yaml)
    snap = build_snapshot(
        ws, crew_name=cfg.name, backend=cfg.backend,
        goal_label=_goal_label_for_logs(ws),
    )

    if as_json:
        import json

        console.print_json(json.dumps(snap.to_dict()))
        return 0

    table = Table(title=f"crewd status — {cfg.name}")
    table.add_column("field"); table.add_column("value")
    table.add_row("workspace", snap.workspace_root)
    table.add_row("remote", cfg.target.remote or "(unset)")
    table.add_row("branch", cfg.target.branch)
    table.add_row("backend", snap.backend)
    table.add_row("goal", snap.goal_label or "[dim]none[/]")

    # Durable journal truth.
    if snap.run_id is None:
        table.add_row("run", "[dim]no dispatch journal yet[/]")
    else:
        table.add_row("run", f"{snap.run_status} ({snap.run_id})")
        table.add_row("routing authority", f"{snap.authority_holder} ({snap.routing_authority})")
        if snap.current_attempt is not None:
            a = snap.current_attempt
            taint = " [red]tainted[/]" if a.get("tainted") else ""
            table.add_row(
                "current attempt",
                f"{a['role']} · {a['state']} · gen {a['generation']}{taint}",
            )
        table.add_row("pending handoffs", str(snap.pending_handoff_count))
        if snap.latest_handoff is not None:
            h = snap.latest_handoff
            present = [k for k in ("evidence", "changed", "remaining", "disagreement", "blocker")
                       if h[k].get("present")]
            table.add_row(
                "latest handoff",
                f"{h['role']} → {h['outcome_class']}"
                + (f" · {h['reason']}" if h["reason"] else "")
                + (f"  [dim](fields: {', '.join(present)})[/]" if present else ""),
            )
        if snap.latest_decision is not None:
            d = snap.latest_decision
            table.add_row(
                "latest lead decision",
                f"{d['kind']}" + (f" → {d['role']}" if d["role"] else ""),
            )
        if snap.wake_condition:
            table.add_row("wake condition", snap.wake_condition)
        if snap.human_blocker:
            table.add_row("human blocker", snap.human_blocker)

    # Lower-authority controls / liveness.
    if snap.daemon_pid is not None:
        alive = "[green]running[/]" if snap.daemon_alive else "[red]dead (stale PID)[/]"
        table.add_row("daemon", f"PID {snap.daemon_pid} ({alive})")
    else:
        table.add_row("daemon", "[dim]not running[/]")
    table.add_row("stopped", "yes" if snap.stopped else "no")
    table.add_row("paused", snap.paused_reason or "no")
    if snap.exit_reason:
        table.add_row("last exit-reason", snap.exit_reason)

    # Public-write durability + operator inbox delivery (issue #29 observability).
    if snap.public_writes is not None:
        pw = snap.public_writes
        pending = pw.get("pending", 0)
        val = f"{pw.get('verified', 0)} verified"
        if pending:
            val += f", [yellow]{pending} unverified[/]"
        table.add_row("public writes", val)
        for detail in pw.get("pending_detail", []):
            provenance = (
                f"{detail.get('repository') or '?'} "
                f"{detail.get('goal') or '?'} task #{detail.get('task') or '?'} "
                f"PR #{detail.get('pr') or '?'}"
            )
            retry = (
                f"backoff {detail.get('backoff_seconds', 0)}s"
                + (
                    f", next {detail['next_retry_at']}"
                    if detail.get("next_retry_at") else ""
                )
            )
            table.add_row(
                f"  write {detail['id']}",
                f"{detail.get('phase')} · {provenance} · "
                f"closure PR #{detail.get('closure_pr') or '?'} · "
                f"{detail.get('action')} · attempts {detail.get('attempts')} · "
                f"{retry} · {detail.get('last_error') or 'no error'}",
            )
    if snap.inbox:
        delivered = sum(v.get("delivering", 0) for v in snap.inbox.values())
        processed = sum(v.get("processed", 0) for v in snap.inbox.values())
        pending_in = sum(v.get("pending", 0) for v in snap.inbox.values())
        table.add_row(
            "operator inbox",
            f"{pending_in} pending · {delivered} delivering · {processed} processed",
        )
    if snap.recovery_action:
        table.add_row("recovery", snap.recovery_action)

    for r in ROLES:
        if r in cfg.roles:
            table.add_row(f"  {r}", f"{cfg.roles[r].model} ({cfg.roles[r].family})")

    console.print(table)

    if snap.contradictions:
        console.print("[yellow]⚠ inconsistent state detected:[/]")
        for c in snap.contradictions:
            console.print(f"  [yellow]•[/] {c}")
    console.print(f"[blue]next action[/] ([bold]{snap.next_action.value}[/]): {snap.next_action_detail}")
    return 0


def cmd_resume(workspace: Path) -> int:
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    cleared = False
    if ws.is_stopped() or ws.is_paused():
        ws.resume()
        cleared = True
        console.print("[green]✓[/] STOPPED/PAUSED sentinels cleared")
    # The explicit resume workflow is the ONLY path allowed to reactivate a
    # durable paused/waiting/interrupted/stopped dispatcher run (a plain `crewd
    # run` must not, so a human blocker is never silently erased).
    resumed = _resume_dispatch_run(ws)
    if resumed:
        console.print("[green]✓[/] dispatcher run resumed → active")
    if not cleared and not resumed:
        console.print("[blue]not stopped or paused — nothing to do[/]")
    return 0


def _resume_dispatch_run(ws: Workspace) -> bool:
    """Transition a durable paused/waiting/interrupted/stopped run back to active.

    Returns True iff a run was actually resumed. No-op (returns False) when there
    is no dispatch journal yet or the run is active/terminal.
    """
    from .dispatcher import DecisionError, Dispatcher, RunStatus

    db = ws.state_dir / "dispatch.db"
    if not db.exists():
        return False
    label = _goal_label_for_logs(ws) or "goal:v1"
    disp = Dispatcher(db)
    try:
        run = disp.start_or_resume_run(label)
        resumable = {
            RunStatus.PAUSED.value,
            RunStatus.WAITING.value,
            RunStatus.INTERRUPTED.value,
            RunStatus.STOPPED.value,
        }
        if run.status in resumable:
            disp.resume_run(run.id)
            return True
        return False
    except (DecisionError, KeyError):
        return False
    finally:
        disp.close()


def cmd_logs(workspace: Path, role: str | None, cycle: int | None, tail: int, follow: bool) -> int:
    """Show role logs.

    No args: list recent logs across all roles.
    --role X: list/show logs for role X.
    --role X --cycle N: print that specific log.
    --tail N: tail last N lines.
    --follow: tail -f the latest log for --role.
    """
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1

    logs_root = ws.state_dir / "logs"
    current_goal_label = _goal_label_for_logs(ws)
    if not logs_root.exists():
        console.print("[yellow]no logs yet[/]")
        return 0

    if role and cycle is not None:
        if not current_goal_label:
            console.print("[red]current goal label unknown; no namespaced logs available[/]")
            return 1
        path = ws.log_file(role, cycle, current_goal_label)
        if not path.exists():
            console.print(f"[red]not found:[/] {path}")
            return 1
        _print_tail(path, tail)
        return 0

    if role and follow:
        # Tail the latest log for the role
        rdir = _latest_role_logs_dir(logs_root, role)
        latest = _latest_log(rdir)
        if not latest:
            console.print(f"[yellow]no logs for {role}[/]")
            return 0
        console.print(f"[blue]tailing[/] {latest}  (Ctrl-C to stop)")
        try:
            subprocess.run(["tail", "-n", str(tail), "-F", str(latest)])
        except KeyboardInterrupt:
            pass
        return 0

    if role:
        rdir = _latest_role_logs_dir(logs_root, role)
        latest = _latest_log(rdir)
        if not latest:
            console.print(f"[yellow]no logs for {role}[/]")
            return 0
        _print_tail(latest, tail)
        return 0

    # No role: list recent across all roles
    table = Table(title="recent logs")
    table.add_column("goal"); table.add_column("role"); table.add_column("cycle"); table.add_column("size"); table.add_column("path")
    rows = []
    for goal_dir in sorted(logs_root.iterdir()):
        if not goal_dir.is_dir():
            continue
        for r in ROLES:
            rdir = goal_dir / r
            if not rdir.exists():
                continue
            for f in sorted(rdir.glob("*.log")):
                rows.append((goal_dir.name, r, f.stem, f.stat().st_size, f))
    rows.sort(key=lambda x: (x[2], x[1], x[0]), reverse=True)
    for goal_name, role_, cyc, sz, p in rows[:20]:
        table.add_row(goal_name, role_, cyc, f"{sz}B", str(p))
    console.print(table)
    return 0


def _latest_log(rdir: Path) -> Path | None:
    if not rdir.exists():
        return None
    files = sorted(rdir.glob("*.log"))
    return files[-1] if files else None


def _latest_role_logs_dir(logs_root: Path, role: str) -> Path:
    candidates: list[tuple[float, Path]] = []
    if not logs_root.exists():
        return logs_root / role
    for goal_dir in logs_root.iterdir():
        if not goal_dir.is_dir():
            continue
        rdir = goal_dir / role
        if not rdir.exists():
            continue
        latest = _latest_log(rdir)
        if latest:
            candidates.append((latest.stat().st_mtime, rdir))
    if not candidates:
        return logs_root / role
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def _print_tail(path: Path, n: int) -> None:
    lines = path.read_text(errors="replace").splitlines()
    for line in lines[-n:]:
        console.print(line, highlight=False)


# ─────────────────────────── list / cd ───────────────────────────
def cmd_list(prune: bool) -> int:
    """List registered workspaces."""
    if prune:
        removed = registry.prune_missing()
        for p in removed:
            console.print(f"[yellow]pruned[/] {p}")
    entries = registry.all_workspaces()
    if not entries:
        console.print("[yellow]no workspaces registered[/]  (run [bold]crewd init <path>[/])")
        return 0
    table = Table(title="crewd workspaces")
    table.add_column("name"); table.add_column("repo"); table.add_column("path"); table.add_column("status")
    for w in entries:
        ws = Workspace(Path(w["path"]))
        if not ws.is_initialized():
            status = "[red]missing[/]"
        elif ws.is_stopped():
            status = "[yellow]stopped[/]"
        else:
            status = f"cycle {ws.read_cycle()}"
        table.add_row(w["name"], w.get("repo") or "-", w["path"], status)
    console.print(table)
    return 0


def cmd_cd(name: str) -> int:
    """Print absolute path of a registered workspace by name (for shell `cd $(crewd cd foo)`)."""
    entry = registry.find(name)
    if not entry:
        console.print(f"[red]no workspace matching[/] {name}")
        return 1
    print(entry["path"])
    return 0


# ─────────────────────────── talk ───────────────────────────
def cmd_talk(workspace: Path, role: str, message: str) -> int:
    """Append a message from the human operator to a role's inbox file.

    The inbox file lives at state/inbox/<role>.md and the role reads it on its
    next tick (we instruct the agent in its prompt to consume + clear it).
    This is the operator's way to nudge a role without touching GitHub issues.
    """
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    if role not in ROLES:
        console.print(f"[red]unknown role:[/] {role}  (one of {ROLES})")
        return 1
    inbox = ws.state_dir / "inbox" / f"{role}.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(inbox, "a") as f:
        f.write(f"\n---\n## [operator @ {ts}]\n{message}\n")
    console.print(f"[green]✓[/] queued message for [bold]{role}[/] → {inbox}")
    return 0


# ─────────────────────────── inbox ───────────────────────────
INBOX_PRIORITIES = ("OVERRIDE", "ADVICE", "INFO")


def cmd_inbox_append(workspace: Path, role: str, priority: str, message: str) -> int:
    """Append a properly-prefixed line to a role's inbox file."""
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    if role not in ROLES:
        console.print(f"[red]unknown role:[/] {role}  (one of {ROLES})")
        return 1
    pr = priority.upper()
    if pr not in INBOX_PRIORITIES:
        console.print(f"[red]unknown priority:[/] {priority}  (one of {INBOX_PRIORITIES})")
        return 1
    inbox = ws.state_dir / "inbox" / f"{role}.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{pr}] {ts} {message}\n"
    with open(inbox, "a") as f:
        f.write(line)
    console.print(f"[green]✓[/] appended [{pr}] message for [bold]{role}[/] → {inbox}")
    return 0


# ─────────────────────────── tick ───────────────────────────
def cmd_tick(workspace: Path, role: str, auto_render: bool = True) -> int:
    """Force-run a single tick for one role, ignoring the loop schedule.

    Equivalent to `crewd run --role <role>` but reads better as an imperative
    operator action ("tick the worker right now").
    """
    return cmd_run(workspace, once=False, role=role, auto_render=auto_render)


# ─────────────────────────── new-goal ───────────────────────────
def _close_label_issues(repo: str, label: str) -> tuple[int, list[str]]:
    """Close all open issues with the given label. Returns (closed_count, errors)."""
    errors: list[str] = []
    closed = 0
    try:
        r = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--label", label,
             "--state", "open", "--json", "number", "--limit", "200"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode != 0:
            errors.append(f"gh issue list failed: {r.stderr.strip()}")
            return 0, errors
        import json as _json
        try:
            issues = _json.loads(r.stdout or "[]")
        except _json.JSONDecodeError as e:
            errors.append(f"gh json decode failed: {e}")
            return 0, errors
        for it in issues:
            num = it.get("number")
            if num is None:
                continue
            cr = subprocess.run(
                ["gh", "issue", "close", str(num), "--repo", repo,
                 "--reason", "not planned",
                 "--comment", f"Closed: superseded by new goal epoch (prior label `{label}`)."],
                capture_output=True, text=True, check=False,
            )
            if cr.returncode == 0:
                closed += 1
            else:
                errors.append(f"gh issue close #{num} failed: {cr.stderr.strip()}")
    except FileNotFoundError:
        errors.append("gh CLI not found on PATH")
    except Exception as e:  # pragma: no cover
        errors.append(f"unexpected error: {e}")
    return closed, errors


def cmd_new_goal(workspace: Path, from_path: Path) -> int:
    """Bump goal epoch: replace GOAL.md, close prior labeled issues, reset state."""
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    src = from_path if from_path.is_absolute() else (Path.cwd() / from_path)
    if not src.exists():
        console.print(f"[red]source GOAL file not found:[/] {src}")
        return 1
    cfg = CrewConfig.load(ws.crew_yaml)

    # Determine next version
    prior_label = None
    if ws.goal_json.exists():
        try:
            prior = GoalState.load(ws.goal_json)
            next_version = prior.version + 1
            prior_label = prior.label
        except Exception:
            next_version = 1
    else:
        next_version = 1

    # Archive prior GOAL.md before overwriting
    if ws.goal_md.exists() and prior_label:
        archive_name = f"GOAL.v{next_version - 1}.md"
        archive_path = ws.state_dir / archive_name
        archive_path.write_text(ws.goal_md.read_text())
        console.print(f"[blue]ℹ[/] archived prior GOAL.md → {archive_path}")

    # Copy/replace GOAL.md if src is different file
    if src.resolve() != ws.goal_md.resolve():
        ws.goal_md.write_text(src.read_text())
    new_sha = sha256_file(ws.goal_md)

    # Close prior labeled issues (best-effort)
    if prior_label and cfg.target.remote:
        for lbl in (prior_label,):  # close issues bearing the prior epoch label
            closed, errs = _close_label_issues(cfg.target.remote, lbl)
            console.print(f"[blue]closed {closed} issues labeled[/] {lbl}")
            for e in errs:
                console.print(f"  [yellow]warn:[/] {e}")

    # Remove STOPPED + exit-reason
    ws.resume()
    if ws.exit_reason_file.exists():
        ws.exit_reason_file.unlink()

    # Write new GoalState
    new_label = f"goal:v{next_version}"
    new_state = GoalState(version=next_version, goal_md_sha256=new_sha, label=new_label, cycles=0)
    new_state.save(ws.goal_json)
    # Reset legacy cycle.txt to 0 too
    ws.write_cycle(0)

    # Re-render agent files with new label
    _render_agent_files(ws, cfg, goal_label=new_label)

    # Append [OVERRIDE] line to ALL role inboxes so every agent re-grounds
    # to live `gh` state on its next tick (defense-in-depth against stale
    # session memory from prior epochs).
    inbox_dir = ws.state_dir / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for role in ROLES:
        if role not in cfg.roles:
            continue
        if role == "lead":
            body = (
                f"\n---\n## [OVERRIDE @ {ts}]\n"
                f"New goal epoch: **{new_label}** (v{next_version}). "
                f"GOAL.md updated from `{src}`. "
                f"Re-plan from scratch using label `{new_label}` for all new issues.\n"
            )
        else:
            body = (
                f"\n---\n## [OVERRIDE @ {ts}]\n"
                f"New goal epoch: **{new_label}** (v{next_version}). "
                f"IGNORE any session memory tied to a prior `goal:vX` label. "
                f"Re-query live `gh issue list --label crewd:task --state open` and "
                f"`gh pr list --state open` to discover work; epoch is informational only.\n"
            )
        with open(inbox_dir / f"{role}.md", "a") as f:
            f.write(body)

    console.print(f"[green]✓[/] new goal epoch [bold]{new_label}[/] (v{next_version})")
    console.print(f"  GOAL.md sha: {new_sha[:12]}…")
    console.print(f"  inbox notices queued in: {inbox_dir}")
    return 0
