"""Shared pytest fixtures."""
from __future__ import annotations
from pathlib import Path
import pytest

from crewd.workspace import Workspace
from crewd.config import default_config


@pytest.fixture(autouse=True)
def _disable_public_bus_by_default(monkeypatch):
    """Disable the default-on public-bus gate for the general test suite.

    A normal ``crewd run`` now wires a real ``CliGitHubClient`` public-bus gate
    (issue #29), which would reach the network. The vast majority of tests
    exercise dispatcher/orchestrator mechanics unrelated to that boundary and set
    up no GitHub record, so they run with the kill-switch set. Tests that assert
    the production enforcement explicitly ``monkeypatch.delenv`` this and inject a
    deterministic fake GitHub client via ``commands._make_github_client``.
    """
    monkeypatch.setenv("CREWD_DISABLE_PUBLIC_BUS", "1")


@pytest.fixture
def tmp_ws(tmp_path: Path) -> Workspace:
    """Initialized workspace at tmp_path/ws with target attached."""
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    ws = Workspace(ws_root)
    ws.ensure_skeleton()
    cfg = default_config(name="testcrew", repo="acme/widget")
    cfg.save(ws.crew_yaml)
    ws.goal_md.write_text("# GOAL\n\nDo the thing.\n")
    # Fake repo dir so doctor / run don't bail
    co = ws.repo_dir(cfg.target.repo)
    co.mkdir(parents=True, exist_ok=True)
    # Create fake per-role worktrees
    for role in ("lead", "advisory", "worker", "verifier"):
        ws.role_worktree(role).mkdir(parents=True, exist_ok=True)
    return ws
