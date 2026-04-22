"""crew.yaml schema and Workspace path conventions."""
from __future__ import annotations
from pathlib import Path
from typing import Literal
import hashlib
import json
import uuid
from datetime import datetime, timezone
import yaml
from pydantic import BaseModel, Field


ROLES = ("lead", "worker", "verifier", "advisory")


class RoleConfig(BaseModel):
    model: str
    family: str  # e.g. "claude", "gpt" — used to enforce worker≠verifier family
    per_tick_timeout: int | None = None  # override loop.per_tick_timeout for this role


class TargetConfig(BaseModel):
    repo: str | None = None  # "owner/name", None until attached
    branch: str = "main"
    checkout: str = "./checkout"  # relative to workspace


class LoopConfig(BaseModel):
    sleep_secs: int = 60
    per_tick_timeout: int = 900
    max_cycles: int = 0  # 0 = forever


class CrewConfig(BaseModel):
    name: str
    target: TargetConfig = Field(default_factory=TargetConfig)
    goal_file: str = "./GOAL.md"
    roles: dict[str, RoleConfig]
    loop: LoopConfig = Field(default_factory=LoopConfig)
    backend: Literal["copilot"] = "copilot"

    @classmethod
    def load(cls, path: Path) -> "CrewConfig":
        with open(path) as f:
            return cls.model_validate(yaml.safe_load(f))

    def save(self, path: Path) -> None:
        with open(path, "w") as f:
            yaml.safe_dump(
                self.model_dump(mode="json"),
                f,
                sort_keys=False,
                default_flow_style=False,
            )

    def validate_families(self) -> list[str]:
        """Return list of error messages, empty if OK."""
        errs = []
        if "worker" in self.roles and "verifier" in self.roles:
            if self.roles["worker"].family == self.roles["verifier"].family:
                errs.append(
                    f"worker.family ({self.roles['worker'].family}) == "
                    f"verifier.family — verifier becomes a rubber stamp. "
                    "Pick different model families."
                )
        return errs


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class GoalState(BaseModel):
    """Per-goal epoch metadata. Stored at state/goal.json.

    Each new goal bumps `version` and gets a fresh `label` (e.g. ``goal:v3``).
    Cycle counter is per-goal: a new goal resets ``cycles`` to 0.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    version: int = 1
    goal_md_sha256: str = ""
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    label: str = "goal:v1"
    cycles: int = 0

    @classmethod
    def load(cls, path: Path) -> "GoalState":
        with open(path) as f:
            return cls.model_validate(json.load(f))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.model_dump(mode="json"), f, indent=2, sort_keys=False)
            f.write("\n")

    @classmethod
    def for_version(cls, version: int, goal_md_sha: str) -> "GoalState":
        return cls(
            version=version,
            goal_md_sha256=goal_md_sha,
            label=f"goal:v{version}",
            cycles=0,
        )


def default_config(name: str, repo: str | None = None) -> CrewConfig:
    return CrewConfig(
        name=name,
        target=TargetConfig(repo=repo),
        roles={
            "lead": RoleConfig(model="claude-opus-4.7", family="claude"),
            "worker": RoleConfig(model="gpt-5.4", family="gpt"),
            "verifier": RoleConfig(model="claude-opus-4.7", family="claude"),
            "advisory": RoleConfig(model="gpt-5.2", family="gpt"),
        },
    )
