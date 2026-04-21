"""Command implementations. Each command is a function called from cli.py."""
from __future__ import annotations
from pathlib import Path
import subprocess
import signal
import sys
import time
from rich.console import Console
from rich.table import Table

from .config import CrewConfig, default_config, ROLES
from .workspace import Workspace
from .templates_render import render, write_if_absent
from .backends import get_backend

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

    console.print(f"[green]✓[/] initialized workspace at [bold]{path}[/]")
    console.print(f"  edit [cyan]{ws.goal_md}[/] then run: [bold]crewd attach <owner/repo>[/] (or pass --repo at init)")
    if repo:
        console.print(f"  attached to [bold]{repo}[/] — run [bold]crewd doctor[/] next")
    return 0


def _render_agent_files(ws: Workspace, cfg: CrewConfig) -> None:
    ctx = {
        "workspace_name": cfg.name,
        "target_repo": cfg.target.repo,
        "worker_model": cfg.roles["worker"].model if "worker" in cfg.roles else "?",
        "verifier_model": cfg.roles["verifier"].model if "verifier" in cfg.roles else "?",
        "advisory_model": cfg.roles["advisory"].model if "advisory" in cfg.roles else "?",
        "worker_family": cfg.roles["worker"].family if "worker" in cfg.roles else "?",
        "verifier_family": cfg.roles["verifier"].family if "verifier" in cfg.roles else "?",
    }
    for role in ROLES:
        if role not in cfg.roles:
            continue
        agent_path = ws.agent_file(role)
        rendered = render(
            f"agents/{role}.agent.md.j2",
            role_model=cfg.roles[role].model,
            **ctx,
        )
        # Always (re)write — these are derived from cfg + templates
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text(rendered)


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
    _render_agent_files(ws, cfg)  # refresh agent.md with repo name

    co = ws.checkout_dir(cfg.target.checkout)
    if clone and not co.exists():
        console.print(f"[blue]cloning[/] {repo} → {co}")
        rc = subprocess.run(
            ["gh", "repo", "clone", repo, str(co), "--", "-b", cfg.target.branch],
            check=False,
        ).returncode
        if rc != 0:
            console.print(f"[yellow]clone failed (rc={rc}); attach config saved anyway[/]")
    console.print(f"[green]✓[/] attached to [bold]{repo}[/] @ {cfg.target.branch}")
    return 0


# ─────────────────────────── doctor ───────────────────────────
def cmd_doctor(workspace: Path) -> int:
    ws = Workspace(workspace.resolve())
    errs: list[str] = []
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    cfg = CrewConfig.load(ws.crew_yaml)

    # Backend health
    backend = get_backend(cfg.backend)
    errs += backend.doctor()

    # gh
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if r.returncode != 0:
            errs.append("gh CLI not authenticated. Run: gh auth login")
    except FileNotFoundError:
        errs.append("gh CLI not found on PATH.")

    # Family check
    errs += cfg.validate_families()

    # Target attached?
    if not cfg.target.repo:
        errs.append("no target repo attached. Run: crewd attach <owner/repo>")
    else:
        co = ws.checkout_dir(cfg.target.checkout)
        if not co.exists():
            errs.append(f"target checkout missing at {co}. Run: crewd attach {cfg.target.repo} --clone")

    # Goal present and non-template?
    if not ws.goal_md.exists():
        errs.append("GOAL.md missing.")
    else:
        body = ws.goal_md.read_text()
        if "Replace this file with your concrete goal" in body:
            errs.append("GOAL.md still has template placeholder. Edit it before running.")

    # Agent files
    for role in cfg.roles:
        if not ws.agent_file(role).exists():
            errs.append(f"agents/{role}.agent.md missing — re-run init or attach.")

    # Print table
    table = Table(title="crewd doctor", show_lines=False)
    table.add_column("check"); table.add_column("status")
    table.add_row("backend", cfg.backend)
    table.add_row("target", cfg.target.repo or "(unset)")
    table.add_row("workspace", str(ws.root))
    table.add_row("checkout", str(ws.checkout_dir(cfg.target.checkout)))
    table.add_row("families", "OK" if not cfg.validate_families() else "[red]MISMATCH[/]")
    table.add_row("stopped", "yes" if ws.is_stopped() else "no")
    table.add_row("cycle", str(ws.read_cycle()))
    console.print(table)

    if errs:
        console.print("\n[red]issues:[/]")
        for e in errs:
            console.print(f"  • {e}")
        return 1
    console.print("\n[green]✓ all checks passed[/]")
    return 0


# ─────────────────────────── goal ───────────────────────────
def cmd_goal(workspace: Path, edit: bool) -> int:
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    if edit:
        editor = subprocess.os.environ.get("EDITOR", "vi")
        subprocess.run([editor, str(ws.goal_md)])
        return 0
    console.print(ws.goal_md.read_text())
    return 0


# ─────────────────────────── run ───────────────────────────
class _LoopController:
    """Holds state for the foreground loop. Allows clean signal handling."""

    def __init__(self, ws: Workspace, cfg: CrewConfig, backend, cwd: Path):
        self.ws = ws
        self.cfg = cfg
        self.backend = backend
        self.cwd = cwd
        self._interrupted = False

    def request_stop(self, signum, frame):
        if self._interrupted:
            console.print("\n[red]double signal — exiting hard[/]")
            sys.exit(130)
        console.print(f"\n[yellow]signal {signum} received — finishing current tick then stopping[/]")
        self._interrupted = True

    def loop(self, once: bool) -> int:
        cycle = self.ws.read_cycle()
        while True:
            if self.ws.is_stopped():
                console.print(f"[yellow]STOPPED sentinel present; exiting at cycle {cycle}[/]")
                return 0
            if self._interrupted:
                console.print("[yellow]interrupted; exiting cleanly[/]")
                return 0
            cycle += 1
            self.ws.write_cycle(cycle)
            console.print(f"\n[bold cyan]── cycle {cycle} ──[/]")
            for r in ROLES:
                if r not in self.cfg.roles:
                    continue
                if self.ws.is_stopped() or self._interrupted:
                    break
                _tick_role(self.ws, self.cfg, self.backend, r, cycle, self.cwd)
            if once:
                return 0
            if self.cfg.loop.max_cycles and cycle >= self.cfg.loop.max_cycles:
                console.print(f"[blue]reached max_cycles={self.cfg.loop.max_cycles}[/]")
                return 0
            # Sleep in 1-second slices so signals/STOPPED are picked up promptly
            for _ in range(self.cfg.loop.sleep_secs):
                if self._interrupted or self.ws.is_stopped():
                    break
                time.sleep(1)


def cmd_run(workspace: Path, once: bool, role: str | None) -> int:
    """Foreground loop. Walks roles in fixed order each cycle, sleeping between cycles.

    --once: run a single cycle then exit.
    --role X: tick only role X (single tick), ignore loop.
    """
    ws = Workspace(workspace.resolve())
    if not ws.is_initialized():
        console.print(f"[red]no workspace at[/] {workspace}")
        return 1
    cfg = CrewConfig.load(ws.crew_yaml)

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

    co = ws.checkout_dir(cfg.target.checkout)
    if not co.exists():
        console.print(f"[red]target checkout missing:[/] {co}. Run [bold]crewd attach --clone[/].")
        return 2

    if role:
        cycle = ws.read_cycle()
        return _tick_role(ws, cfg, backend, role, cycle, co)

    ws.resume()  # clear STOPPED if present (run is explicit intent)
    ctrl = _LoopController(ws, cfg, backend, co)
    # Install signal handlers (SIGINT, SIGTERM) — graceful stop
    prev_int = signal.signal(signal.SIGINT, ctrl.request_stop)
    prev_term = signal.signal(signal.SIGTERM, ctrl.request_stop)
    try:
        return ctrl.loop(once)
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)


def _tick_role(ws: Workspace, cfg: CrewConfig, backend, role: str, cycle: int, cwd: Path) -> int:
    if role not in cfg.roles:
        console.print(f"[red]unknown role:[/] {role}")
        return 1
    role_cfg = cfg.roles[role]
    cfg_dir = ws.role_cfg_dir(role)
    log_path = ws.log_file(role, cycle)
    first_run = not (cfg_dir / "session-state").exists()

    prompt = (
        f"This is cycle {cycle}. Read the latest GitHub issues + comments in "
        f"`{cfg.target.repo}` and execute your role's responsibilities. "
        f"GOAL.md is at `{ws.goal_md}`. Do one tick and stop."
    )
    add_dirs = [ws.root]  # workspace as readable context
    console.print(f"  [magenta]{role}[/] ({role_cfg.model}) → {log_path}")
    rc = backend.run_role(
        role=role,
        model=role_cfg.model,
        config_dir=cfg_dir,
        agent_md=ws.agent_file(role),
        add_dirs=add_dirs,
        prompt=prompt,
        log_path=log_path,
        timeout=cfg.loop.per_tick_timeout,
        cwd=cwd,
        first_run=first_run,
    )
    if rc != 0:
        console.print(f"    [yellow]{role} exited rc={rc}[/]")
    return rc


# ─────────────────────────── stop / status ───────────────────────────
def cmd_stop(workspace: Path, reason: str) -> int:
    ws = Workspace(workspace.resolve())
    ws.stop(reason)
    console.print(f"[green]✓[/] STOPPED ({reason})")
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
