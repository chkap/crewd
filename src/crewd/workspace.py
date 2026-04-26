"""Workspace path layout and helpers.

Workspace structure:
  <workspace>/
    crew.yaml              — config
    GOAL.md                — current goal/spec
    state/                 — runtime state
      STOPPED              — sentinel: loop won't tick
      run.pid              — daemon PID (present only when daemon is running)
      cycle.txt            — current cycle counter
      logs/<role>/<cycle>.log
      logs/daemon.log      — daemon stdout/stderr
    cfg/                   — per-role working directory + copilot config
      lead/
        AGENTS.md          — role instructions (Copilot auto-loads from cwd)
        session-state/     — copilot --config-dir session data
        worktree/          — git worktree (isolated repo copy)
      worker/
        AGENTS.md
        session-state/
        worktree/
      verifier/
        AGENTS.md
        session-state/
        worktree/
      advisory/
        AGENTS.md
        session-state/
        worktree/
    repo/                  — main target repo clone (configurable)
"""
from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass
import os


@dataclass
class Workspace:
    root: Path

    @property
    def crew_yaml(self) -> Path:
        return self.root / "crew.yaml"

    @property
    def goal_md(self) -> Path:
        return self.root / "GOAL.md"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def stopped_sentinel(self) -> Path:
        return self.state_dir / "STOPPED"

    @property
    def cycle_file(self) -> Path:
        return self.state_dir / "cycle.txt"

    @property
    def goal_json(self) -> Path:
        return self.state_dir / "goal.json"

    @property
    def exit_reason_file(self) -> Path:
        return self.state_dir / "exit-reason"

    def log_file(self, role: str, cycle: int) -> Path:
        return self.state_dir / "logs" / role / f"{cycle:04d}.log"

    @property
    def cfg_dir(self) -> Path:
        return self.root / "cfg"

    def role_cfg_dir(self, role: str) -> Path:
        return self.cfg_dir / role

    def repo_dir(self, configured: str = "./repo") -> Path:
        p = Path(configured)
        return p if p.is_absolute() else (self.root / p).resolve()

    def role_worktree(self, role: str) -> Path:
        return self.cfg_dir / role / "worktree"

    def ensure_skeleton(self) -> None:
        for d in [self.state_dir, self.state_dir / "logs", self.cfg_dir]:
            d.mkdir(parents=True, exist_ok=True)
        for role in ("lead", "advisory", "worker", "verifier"):
            (self.state_dir / "logs" / role).mkdir(parents=True, exist_ok=True)
            self.role_cfg_dir(role).mkdir(parents=True, exist_ok=True)

    def is_initialized(self) -> bool:
        return self.crew_yaml.exists()

    def read_cycle(self) -> int:
        if not self.cycle_file.exists():
            return 0
        try:
            return int(self.cycle_file.read_text().strip() or "0")
        except ValueError:
            return 0

    def write_cycle(self, n: int) -> None:
        self.cycle_file.write_text(str(n))

    def stop(self, reason: str = "manual") -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.stopped_sentinel.write_text(reason + "\n")

    def resume(self) -> None:
        self.stopped_sentinel.unlink(missing_ok=True)

    def is_stopped(self) -> bool:
        return self.stopped_sentinel.exists()

    # ── PID file (daemon mode) ──

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "run.pid"

    @property
    def daemon_log(self) -> Path:
        return self.state_dir / "logs" / "daemon.log"

    def write_pid(self, pid: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(pid) + "\n")

    def read_pid(self) -> int | None:
        if not self.pid_file.exists():
            return None
        try:
            return int(self.pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def clear_pid(self) -> None:
        self.pid_file.unlink(missing_ok=True)

    def is_daemon_alive(self) -> bool:
        pid = self.read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)  # signal 0 = existence check
            return True
        except OSError:
            return False


def find_workspace(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) looking for a directory with crew.yaml.

    Git-style discovery. Returns the first ancestor directory containing
    crew.yaml, or None if none found before filesystem root.
    """
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / "crew.yaml").exists():
            return candidate
    return None
