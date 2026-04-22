"""Workspace path layout and helpers.

Workspace structure:
  <workspace>/
    crew.yaml              — config
    GOAL.md                — current goal/spec
    agents/                — per-role .agent.md files (role + responsibilities)
      lead.agent.md
      worker.agent.md
      verifier.agent.md
      advisory.agent.md
    state/                 — runtime state
      STOPPED              — sentinel: loop won't tick
      cycle.txt            — current cycle counter
      logs/<role>/<cycle>.log
    cfg/                   — per-role copilot --config-dir target
      lead/
      worker/
      verifier/
      advisory/
    checkout/              — target repo clone (configurable)
"""
from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass


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
    def agents_dir(self) -> Path:
        return self.root / "agents"

    def agent_file(self, role: str) -> Path:
        return self.agents_dir / f"{role}.agent.md"

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

    def checkout_dir(self, configured: str = "./checkout") -> Path:
        p = Path(configured)
        return p if p.is_absolute() else (self.root / p).resolve()

    def ensure_skeleton(self) -> None:
        for d in [self.agents_dir, self.state_dir, self.state_dir / "logs", self.cfg_dir]:
            d.mkdir(parents=True, exist_ok=True)
        for role in ("lead", "worker", "verifier", "advisory"):
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
