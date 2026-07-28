"""crew.yaml schema and Workspace path conventions."""
from __future__ import annotations
from pathlib import Path
from typing import Any, Literal
import hashlib
import json
import uuid
from datetime import datetime, timezone
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


ROLES = ("lead", "advisory", "worker", "verifier")


# The retired ``copilot -p`` subprocess transport (issue #17); a legacy workspace
# still carrying it must be migrated to the SDK backend by ``crewd refresh``.
RETIRED_BACKEND = "copilot"
CURRENT_BACKEND = "copilot-sdk"


class RoleConfig(BaseModel):
    # ``extra="allow"`` preserves unknown user keys across a load→save roundtrip
    # (issue #28): a refresh/migration must never silently drop configuration it
    # does not recognise.
    model_config = ConfigDict(extra="allow")

    model: str
    family: str  # e.g. "claude", "gpt" — used to enforce worker≠verifier family
    per_tick_timeout: int | None = None  # override loop.per_tick_timeout for this role


class TargetConfig(BaseModel):
    """Target repo config.

    Fields:
      - ``remote``: GitHub ``owner/name`` identifier (was ``repo`` pre-v2).
      - ``branch``: default branch.
      - ``repo``:   local clone path under the workspace (was ``checkout`` pre-v2).

    Backwards compatibility: old keys ``repo`` (for remote) and ``checkout``
    (for local path) are still accepted on load and transparently mapped.
    The legacy-``repo``→``remote`` heuristic fires when the value contains
    ``/`` and does not start with ``.`` or ``/`` (i.e. looks like ``owner/name``,
    not a local path). Avoid local clone paths shaped like ``sub/dir`` to
    sidestep this edge case — prefer ``./sub/dir``.
    """

    model_config = ConfigDict(extra="allow")

    remote: str | None = None           # "owner/name", None until attached
    branch: str = "main"
    repo: str = "./repo"                # local clone path, relative to workspace

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        # Legacy: target.checkout (local path) → target.repo
        if "checkout" in d and "repo" not in d:
            d["repo"] = d.pop("checkout")
        elif "checkout" in d and "repo" in d:
            # Ambiguous: if both present, assume old-style where repo = remote.
            # This means: repo holds "owner/name", checkout holds local path.
            if d.get("repo") and "/" in str(d["repo"]) and "remote" not in d:
                d["remote"] = d.pop("repo")
                d["repo"] = d.pop("checkout")
            else:
                d.pop("checkout", None)
        else:
            # No checkout key; but repo may still be an old-style "owner/name" remote
            # (heuristic: contains "/" and no path separator prefix like "./")
            if (
                "remote" not in d
                and isinstance(d.get("repo"), str)
                and "/" in d["repo"]
                and not d["repo"].startswith((".", "/"))
            ):
                d["remote"] = d.pop("repo")
        return d

class LoopConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    sleep_secs: int = 60
    per_tick_timeout: int = 900
    max_cycles: int = 0  # 0 = forever


class CrewConfig(BaseModel):
    # ``extra="allow"`` preserves unknown top-level user keys (e.g. custom
    # tooling/notification settings) across a refresh/migration roundtrip so the
    # upgrade never destroys configuration crewd does not itself consume (#28).
    model_config = ConfigDict(extra="allow")

    name: str
    target: TargetConfig = Field(default_factory=TargetConfig)
    goal_file: str = "./GOAL.md"
    roles: dict[str, RoleConfig]
    loop: LoopConfig = Field(default_factory=LoopConfig)
    # A retired ``copilot`` value is still *accepted on load* so a legacy
    # workspace parses and can be migrated by ``crewd refresh``; it is rejected at
    # run pre-flight until migrated.
    backend: Literal["copilot", "copilot-sdk"] = "copilot-sdk"
    # Extra host directories (beyond the workspace + role worktree) that every
    # role's agent is granted file access to via the backend's --add-dir flag.
    # Use this to expose deployment checkouts, persistent data dirs, or other
    # paths that live outside the crew workspace. Relative entries resolve
    # against the workspace root; absolute paths are used as-is. Only existing
    # directories are passed through — missing entries are silently skipped so a
    # stale path can't break a tick.
    extra_add_dirs: list[str] = Field(default_factory=list)

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

    def pending_migrations(self) -> list[str]:
        """Describe schema migrations this config still needs, without mutating.

        Used by ``crewd doctor``/run pre-flight to report a *migration-required*
        state distinctly from a healthy or missing-SDK one (#28). An empty list
        means the schema is current.
        """
        pending: list[str] = []
        if self.backend == RETIRED_BACKEND:
            pending.append(
                f"backend: {RETIRED_BACKEND} (the retired `copilot -p` subprocess "
                f"transport) → {CURRENT_BACKEND}"
            )
        return pending

    def apply_migrations(self) -> list[str]:
        """Mutate this config in place to the current schema.

        Returns a list of human-readable notes for each migration performed
        (empty when already current). Idempotent: a second call after a save is a
        no-op. Unknown user keys are preserved by ``extra="allow"`` and are never
        touched here.
        """
        applied: list[str] = []
        if self.backend == RETIRED_BACKEND:
            self.backend = CURRENT_BACKEND
            applied.append(
                f"backend: {RETIRED_BACKEND} → {CURRENT_BACKEND} "
                "(retired subprocess transport migrated to the Copilot SDK)"
            )
        return applied

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


def default_config(
    name: str, remote: str | None = None, *, repo: str | None = None
) -> CrewConfig:
    # Back-compat: old callers passed ``repo="owner/name"``. New name is ``remote``.
    if remote is None and repo is not None:
        remote = repo
    return CrewConfig(
        name=name,
        target=TargetConfig(remote=remote),
        roles={
            "lead": RoleConfig(model="claude-sonnet-4.6", family="claude"),
            "worker": RoleConfig(model="gpt-5.4", family="gpt"),
            "verifier": RoleConfig(model="claude-sonnet-4.6", family="claude"),
            "advisory": RoleConfig(model="gpt-5.4", family="gpt"),
        },
    )
