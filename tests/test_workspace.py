"""Tests for crewd.workspace — paths, sentinels, cycle counter."""
from __future__ import annotations
from pathlib import Path

from crewd.workspace import Workspace


def test_paths_are_relative_to_root(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    assert ws.crew_yaml == tmp_path / "ws" / "crew.yaml"
    assert ws.goal_md == tmp_path / "ws" / "GOAL.md"
    assert ws.role_cfg_dir("lead") == tmp_path / "ws" / "cfg" / "lead"
    assert ws.log_file("verifier", 7, "goal:v3") == tmp_path / "ws" / "state" / "logs" / "goal-v3" / "verifier" / "0007.log"


def test_ensure_skeleton_creates_dirs(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_skeleton()
    for role in ("lead", "advisory", "worker", "verifier"):
        assert (ws.state_dir / "logs").is_dir()
        assert ws.role_cfg_dir(role).is_dir()


def test_log_dir_sanitizes_goal_label(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    assert ws.log_file("lead", 1, "goal:v12") == tmp_path / "ws" / "state" / "logs" / "goal-v12" / "lead" / "0001.log"


def test_stop_resume_sentinel(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_skeleton()
    assert not ws.is_stopped()
    ws.stop("manual")
    assert ws.is_stopped()
    assert "manual" in ws.stopped_sentinel.read_text()
    ws.resume()
    assert not ws.is_stopped()
    # Idempotent
    ws.resume()
    assert not ws.is_stopped()


def test_cycle_counter(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_skeleton()
    assert ws.read_cycle() == 0
    ws.write_cycle(5)
    assert ws.read_cycle() == 5
    # Bad data → 0
    ws.cycle_file.write_text("not a number\n")
    assert ws.read_cycle() == 0


def test_repo_dir_resolution(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    # Relative
    assert ws.repo_dir("./repo") == (tmp_path / "ws" / "repo").resolve()
    # Absolute
    abs_path = tmp_path / "elsewhere" / "repo"
    assert ws.repo_dir(str(abs_path)) == abs_path


def test_role_worktree_path(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    assert ws.role_worktree("lead") == tmp_path / "ws" / "cfg" / "lead" / "worktree"
    assert ws.role_worktree("worker") == tmp_path / "ws" / "cfg" / "worker" / "worktree"
