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
    cfg = default_config(name=name, repo=repo)
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
        "target_repo": cfg.target.repo,
        "target_branch": cfg.target.branch,
        "worker_model": cfg.roles["worker"].model if "worker" in cfg.roles else "?",
        "verifier_model": cfg.roles["verifier"].model if "verifier" in cfg.roles else "?",
        "advisory_model": cfg.roles["advisory"].model if "advisory" in cfg.roles else "?",
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
        # Write agents/<role>.agent.md (reference/debugging)
        agent_path = ws.agent_file(role)
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text(rendered)
        # Write AGENTS.md into cfg/<role>/ (Copilot auto-loads from cwd)
        role_dir = ws.role_cfg_dir(role)
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "AGENTS.md").write_text(rendered)


def check_and_render(ws: Workspace, cfg: CrewConfig) -> bool:
    """If crew.yaml is newer than any agent.md, or any are missing, re-render.

    Returns True iff a re-render happened. Logs a one-line notice when it does.
    """
    if not ws.crew_yaml.exists():
        return False
    yaml_mtime = ws.crew_yaml.stat().st_mtime
    needs = False
    for role in ROLES:
        if role not in cfg.roles:
            continue
        amd = ws.agent_file(role)
        if not amd.exists() or amd.stat().st_mtime < yaml_mtime:
            needs = True
            break
    if needs:
        _render_agent_files(ws, cfg)
        console.print("[blue]ℹ[/] auto-rendered agents/ from crew.yaml (use --no-auto-render to skip)")
    return needs


# ─────────────────────────── refresh ───────────────────────────
def cmd_refresh(workspace: Path) -> int:
    """Force re-render agents/*.agent.md + AGENTS.md from templates + crew.yaml.

    Also performs workspace migration if needed:
    - Renames checkout/ → repo/ if the old layout is detected
    - Updates crew.yaml checkout path accordingly
    - Creates per-role git worktrees if missing
    """
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    cfg = CrewConfig.load(ws.crew_yaml)

    # ── migrate: checkout/ → repo/ ──
    old_co = (ws.root / "checkout").resolve()
    target_co = (ws.root / "repo").resolve()
    if old_co.exists() and old_co != target_co and not target_co.exists():
        old_co.rename(target_co)
        console.print(f"[blue]ℹ[/] migrated checkout/ → repo/")
    if cfg.target.checkout == "./checkout":
        cfg.target.checkout = "./repo"
        cfg.save(ws.crew_yaml)
        console.print(f"[blue]ℹ[/] updated crew.yaml checkout → ./repo")

    # ── create worktrees if repo exists but worktrees don't ──
    repo = ws.repo_dir(cfg.target.checkout)
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
            console.print(f"  [green]✓[/] {ws.agent_file(role).name}")
            console.print(f"  [green]✓[/] {ws.role_cfg_dir(role) / 'AGENTS.md'}")
    console.print(f"[green]✓[/] refreshed (agents/ + AGENTS.md)")
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
    cfg.target.repo = repo
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

    co = ws.repo_dir(cfg.target.checkout)
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
def _inbox_summary(ws: Workspace, role: str) -> tuple[int, str | None]:
    """Return (count, last_sender) for a role's inbox file. Best-effort parse."""
    inbox = ws.state_dir / "inbox" / f"{role}.md"
    if not inbox.exists():
        return 0, None
    try:
        body = inbox.read_text(errors="replace")
    except Exception:
        return 0, None
    headers = [ln for ln in body.splitlines() if ln.startswith("## [")]
    last = None
    if headers:
        h = headers[-1].lstrip("# ").strip("[] ")
        last = h.split("@", 1)[0].strip() or None
    return len(headers), last


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
        f"backend=[cyan]{cfg.backend}[/], target=[cyan]{cfg.target.repo or '(unset)'}[/])"
    )

    # Roles table
    roles_tbl = Table(title="roles", show_lines=False)
    roles_tbl.add_column("role"); roles_tbl.add_column("model"); roles_tbl.add_column("family")
    roles_tbl.add_column("agent.md"); roles_tbl.add_column("session-state"); roles_tbl.add_column("last log")
    yaml_mtime = ws.crew_yaml.stat().st_mtime
    for role in ROLES:
        if role not in cfg.roles:
            continue
        rc = cfg.roles[role]
        amd = ws.agent_file(role)
        if amd.exists():
            if amd.stat().st_mtime < yaml_mtime:
                amd_str = "[yellow]stale[/]"
                issues.append(("warn", f"agents/{role}.agent.md is older than crew.yaml — re-render needed"))
            else:
                amd_str = "[green]ok[/]"
        else:
            amd_str = "[red]missing[/]"
            issues.append(("error", f"agents/{role}.agent.md missing — run init/attach or `crewd run` (auto-render)"))
        sdir = ws.role_cfg_dir(role) / "session-state"
        if sdir.exists():
            if not sdir.is_dir():
                sess_str = "[red]corrupt[/]"
                issues.append(("error", f"cfg/{role}/session-state exists but is not a directory"))
            else:
                sess_str = "[green]present[/]"
        else:
            sess_str = "[dim]none[/]"
        ldir = ws.state_dir / "logs" / role
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
    in_tbl.add_column("role"); in_tbl.add_column("pending"); in_tbl.add_column("last sender")
    for role in ROLES:
        if role not in cfg.roles:
            continue
        cnt, sender = _inbox_summary(ws, role)
        in_tbl.add_row(role, str(cnt), sender or "—")
    console.print(in_tbl)

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
    if not cfg.target.repo:
        issues.append(("warn", "no target repo attached. Run: crewd attach <owner/repo>"))
    else:
        co = ws.repo_dir(cfg.target.checkout)
        if not co.exists():
            issues.append(("warn", f"target repo clone missing at {co}. Run: crewd attach {cfg.target.repo} --clone"))

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


class _LoopController:
    """Holds state for the foreground loop. Allows clean signal handling."""

    def __init__(self, ws: Workspace, cfg: CrewConfig, backend, goal_state: GoalState):
        self.ws = ws
        self.cfg = cfg
        self.backend = backend
        self.goal_state = goal_state
        self._interrupted = False

    def request_stop(self, signum, frame):
        if self._interrupted:
            console.print("\n[red]double signal — exiting hard[/]")
            sys.exit(130)
        console.print(f"\n[yellow]signal {signum} received — finishing current tick then stopping[/]")
        self._interrupted = True

    def _save_cycle(self, cycle: int) -> None:
        self.goal_state.cycles = cycle
        self.goal_state.save(self.ws.goal_json)
        # Mirror to legacy cycle.txt for back-compat with existing reads
        self.ws.write_cycle(cycle)

    def loop(self, once: bool) -> int:
        cycle = self.goal_state.cycles
        while True:
            if self.ws.is_stopped():
                console.print(f"[yellow]STOPPED sentinel present; exiting at cycle {cycle}[/]")
                _write_exit_reason(self.ws, "goal-complete")
                return 0
            if self._interrupted:
                console.print("[yellow]interrupted; exiting cleanly[/]")
                _write_exit_reason(self.ws, "interrupted")
                return 0
            cycle += 1
            self._save_cycle(cycle)
            console.print(f"\n[bold cyan]── cycle {cycle} ──[/]")
            for r in ROLES:
                if r not in self.cfg.roles:
                    continue
                if self.ws.is_stopped() or self._interrupted:
                    break
                _tick_role(self.ws, self.cfg, self.backend, r, cycle)
            if self.ws.is_stopped():
                console.print(f"[yellow]STOPPED detected after cycle {cycle}[/]")
                _write_exit_reason(self.ws, "goal-complete")
                return 0
            if once:
                return 0
            if self.cfg.loop.max_cycles and cycle >= self.cfg.loop.max_cycles:
                console.print(f"[blue]reached max_cycles={self.cfg.loop.max_cycles}[/]")
                _write_exit_reason(self.ws, "exhausted")
                return 0
            # Sleep in 1-second slices so signals/STOPPED are picked up promptly
            for _ in range(self.cfg.loop.sleep_secs):
                if self._interrupted or self.ws.is_stopped():
                    break
                time.sleep(1)


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

    backend = get_backend(cfg.backend)
    bd_errs = backend.doctor()
    if bd_errs:
        for e in bd_errs:
            console.print(f"[red]backend:[/] {e}")
        return 2

    co = ws.repo_dir(cfg.target.checkout)
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


def cmd_run(workspace: Path, once: bool, role: str | None, auto_render: bool = True) -> int:
    """Foreground loop. Walks roles in fixed order each cycle, sleeping between cycles.

    --once: run a single cycle then exit.
    --role X: tick only role X (single tick), ignore loop.
    auto_render: if True (default), re-render agents/ when crew.yaml is newer.
    """
    result = _preflight(workspace, auto_render)
    if isinstance(result, int):
        return result
    ws, cfg, backend, goal_state = result

    if role:
        cycle = goal_state.cycles
        return _tick_role(ws, cfg, backend, role, cycle)

    # Clear stale exit-reason at start
    if ws.exit_reason_file.exists():
        ws.exit_reason_file.unlink()
    ws.resume()  # clear STOPPED if present (run is explicit intent)
    ctrl = _LoopController(ws, cfg, backend, goal_state)
    # Install signal handlers (SIGINT, SIGTERM) — graceful stop
    prev_int = signal.signal(signal.SIGINT, ctrl.request_stop)
    prev_term = signal.signal(signal.SIGTERM, ctrl.request_stop)
    try:
        return ctrl.loop(once)
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

    # Clear stale state
    if ws.exit_reason_file.exists():
        ws.exit_reason_file.unlink()
    ws.resume()

    ctrl = _LoopController(ws, cfg, backend, goal_state)
    signal.signal(signal.SIGINT, ctrl.request_stop)
    signal.signal(signal.SIGTERM, ctrl.request_stop)
    try:
        rc = ctrl.loop(once)
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
    log_path = ws.log_file(role, cycle)
    first_run = not (cfg_dir / "session-state").exists()

    # cwd = cfg/<role>/ so Copilot auto-loads AGENTS.md from there.
    # The role's git worktree is added via --add-dir for file access.
    cwd = ws.role_cfg_dir(role)
    wt = ws.role_worktree(role)

    prompt = (
        f"This is cycle {cycle}. Read the latest GitHub issues + comments in "
        f"`{cfg.target.repo}` and execute your role's responsibilities. "
        f"GOAL.md is at `{ws.goal_md}`. "
        f"If `{ws.state_dir / 'inbox' / (role + '.md')}` exists, read its messages "
        f"from the human operator FIRST, then truncate that file to empty. "
        f"Do one tick and stop."
    )
    add_dirs = [ws.root]  # workspace as readable context
    if wt.exists():
        add_dirs.append(wt)
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
    )
    if rc != 0:
        console.print(f"    [yellow]{role} exited rc={rc}[/]")
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


def cmd_status(workspace: Path) -> int:
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    cfg = CrewConfig.load(ws.crew_yaml)
    table = Table(title=f"crewd status — {cfg.name}")
    table.add_column("field"); table.add_column("value")
    table.add_row("workspace", str(ws.root))
    table.add_row("repo", cfg.target.repo or "(unset)")
    table.add_row("branch", cfg.target.branch)
    table.add_row("cycle", str(ws.read_cycle()))
    table.add_row("stopped", "yes" if ws.is_stopped() else "no")
    # Daemon status
    pid = ws.read_pid()
    if pid is not None:
        alive = ws.is_daemon_alive()
        status = f"PID {pid} ([green]running[/])" if alive else f"PID {pid} ([red]dead[/])"
        table.add_row("daemon", status)
        if not alive:
            ws.clear_pid()
    else:
        table.add_row("daemon", "[dim]not running[/]")
    table.add_row("backend", cfg.backend)
    for r in ROLES:
        if r in cfg.roles:
            table.add_row(f"  {r}", f"{cfg.roles[r].model} ({cfg.roles[r].family})")
    console.print(table)
    return 0


def cmd_resume(workspace: Path) -> int:
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    if ws.is_stopped():
        ws.resume()
        console.print("[green]✓[/] STOPPED sentinel cleared")
    else:
        console.print("[blue]not stopped — nothing to do[/]")
    return 0


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
    if not logs_root.exists():
        console.print("[yellow]no logs yet[/]")
        return 0

    if role and cycle is not None:
        path = ws.log_file(role, cycle)
        if not path.exists():
            console.print(f"[red]not found:[/] {path}")
            return 1
        _print_tail(path, tail)
        return 0

    if role and follow:
        # Tail the latest log for the role
        rdir = logs_root / role
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
        rdir = logs_root / role
        latest = _latest_log(rdir)
        if not latest:
            console.print(f"[yellow]no logs for {role}[/]")
            return 0
        _print_tail(latest, tail)
        return 0

    # No role: list recent across all roles
    table = Table(title="recent logs")
    table.add_column("role"); table.add_column("cycle"); table.add_column("size"); table.add_column("path")
    rows = []
    for r in ROLES:
        rdir = logs_root / r
        if not rdir.exists():
            continue
        for f in sorted(rdir.glob("*.log")):
            rows.append((r, f.stem, f.stat().st_size, f))
    rows.sort(key=lambda x: (x[1], x[0]), reverse=True)
    for role_, cyc, sz, p in rows[:20]:
        table.add_row(role_, cyc, f"{sz}B", str(p))
    console.print(table)
    return 0


def _latest_log(rdir: Path) -> Path | None:
    if not rdir.exists():
        return None
    files = sorted(rdir.glob("*.log"))
    return files[-1] if files else None


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
    if prior_label and cfg.target.repo:
        for lbl in (prior_label,):  # close issues bearing the prior epoch label
            closed, errs = _close_label_issues(cfg.target.repo, lbl)
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

    # Append [OVERRIDE] line to lead inbox
    inbox = ws.state_dir / "inbox" / "lead.md"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(inbox, "a") as f:
        f.write(
            f"\n---\n## [OVERRIDE @ {ts}]\n"
            f"New goal epoch: **{new_label}** (v{next_version}). "
            f"GOAL.md updated from `{src}`. "
            f"Re-plan from scratch using label `{new_label}` for all new issues.\n"
        )

    console.print(f"[green]✓[/] new goal epoch [bold]{new_label}[/] (v{next_version})")
    console.print(f"  GOAL.md sha: {new_sha[:12]}…")
    console.print(f"  inbox notice queued: {inbox}")
    return 0
