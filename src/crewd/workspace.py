"""Workspace path layout and helpers.

Workspace structure:
  <workspace>/
    crew.yaml              — config
    GOAL.md                — current goal/spec
    state/                 — runtime state
      STOPPED              — sentinel: loop won't tick
      run.pid              — daemon PID (present only when daemon is running)
      cycle.txt            — current cycle counter
      logs/<goal-label>/<role>/<cycle>.log
      logs/daemon.log      — daemon stdout/stderr
    cfg/                   — per-role working directory + copilot config
      lead/
        AGENTS.md          — role instructions (Copilot auto-loads from cwd)
        session-state/     — copilot session data (COPILOT_HOME=cfg/<role>)
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
import re


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

    def goal_log_dirname(self, goal_label: str) -> str:
        """Filesystem-safe directory name for a goal label like ``goal:v3``."""
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", goal_label.strip())
        return safe or "goal-unknown"

    def logs_dir(self, goal_label: str) -> Path:
        return self.state_dir / "logs" / self.goal_log_dirname(goal_label)

    def role_logs_dir(self, role: str, goal_label: str) -> Path:
        return self.logs_dir(goal_label) / role

    def log_file(self, role: str, cycle: int, goal_label: str) -> Path:
        return self.role_logs_dir(role, goal_label) / f"{cycle:04d}.log"

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

    def resolve_extra_dirs(self, entries: list[str]) -> list[Path]:
        """Resolve configured extra_add_dirs to existing absolute directories.

        Relative entries resolve against the workspace root; absolute paths are
        used as-is. Non-existent paths are skipped so a stale config entry can't
        break a tick. Order is preserved and duplicates are removed.
        """
        out: list[Path] = []
        seen: set[Path] = set()
        for entry in entries:
            p = Path(entry)
            resolved = p if p.is_absolute() else (self.root / p)
            resolved = resolved.resolve()
            if resolved in seen:
                continue
            if resolved.is_dir():
                seen.add(resolved)
                out.append(resolved)
        return out

    def ensure_skeleton(self) -> None:
        for d in [self.state_dir, self.state_dir / "logs", self.cfg_dir]:
            d.mkdir(parents=True, exist_ok=True)
        for role in ("lead", "advisory", "worker", "verifier"):
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
