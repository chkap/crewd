"""Tests for goal lifecycle / epoch features."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import json

import pytest

from crewd import commands
from crewd.config import GoalState, sha256_file, CrewConfig
from crewd.workspace import Workspace


# ─── GoalState roundtrip ───
def test_goalstate_save_load_roundtrip(tmp_path: Path):
    p = tmp_path / "goal.json"
    g = GoalState(version=2, goal_md_sha256="abc123", label="goal:v2", cycles=7)
    g.save(p)
    loaded = GoalState.load(p)
    assert loaded.version == 2
    assert loaded.label == "goal:v2"
    assert loaded.cycles == 7
    assert loaded.goal_md_sha256 == "abc123"
    # File is valid JSON
    data = json.loads(p.read_text())
    assert data["version"] == 2


# ─── Migration helper ───
def test_load_or_init_creates_v1_from_existing(tmp_ws: Workspace):
    # Seed legacy cycle.txt
    tmp_ws.write_cycle(5)
    assert not tmp_ws.goal_json.exists()
    state = commands._load_or_init_goal_state(tmp_ws)
    assert state.version == 1
    assert state.label == "goal:v1"
    assert state.cycles == 5
    assert state.goal_md_sha256 == sha256_file(tmp_ws.goal_md)
    assert tmp_ws.goal_json.exists()


def test_load_or_init_returns_existing(tmp_ws: Workspace):
    GoalState(version=3, label="goal:v3", cycles=99, goal_md_sha256="zz").save(tmp_ws.goal_json)
    state = commands._load_or_init_goal_state(tmp_ws)
    assert state.version == 3
    assert state.cycles == 99


# ─── SHA mismatch detection ───
def test_cmd_run_detects_goal_sha_mismatch(tmp_ws: Workspace, monkeypatch):
    # Pre-seed goal.json with wrong sha
    GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="deadbeef").save(tmp_ws.goal_json)
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert rc == 2
    # Backend should not have been called
    assert backend.calls == []


# ─── Exit reason files ───
def test_exit_reason_written_on_stopped(tmp_ws: Workspace, monkeypatch):
    backend = _StoppingBackend()
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.exit_reason_file.exists()
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"


def test_exit_reason_exhausted_on_max_cycles(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.sleep_secs = 0
    cfg.loop.max_cycles = 1
    cfg.save(tmp_ws.crew_yaml)
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "exhausted"


# ─── new-goal command ───
def test_cmd_new_goal_initial_creates_v1(tmp_ws: Workspace, monkeypatch):
    # No prior goal.json
    src = tmp_ws.root / "NEW_GOAL.md"
    src.write_text("# new\n\nfresh content\n")
    # No subprocess calls expected (no prior label)
    monkeypatch.setattr(commands.subprocess, "run", _explode_subprocess)
    rc = commands.cmd_new_goal(tmp_ws.root, src)
    assert rc == 0
    state = GoalState.load(tmp_ws.goal_json)
    assert state.version == 1
    assert state.label == "goal:v1"
    assert state.cycles == 0
    assert "fresh content" in tmp_ws.goal_md.read_text()


def test_cmd_new_goal_increments_and_closes_issues(tmp_ws: Workspace, monkeypatch):
    # Seed prior state at v2
    GoalState(version=2, label="goal:v2", cycles=42, goal_md_sha256="old").save(tmp_ws.goal_json)
    src = tmp_ws.root / "NEW_GOAL.md"
    src.write_text("# v3\n")

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        # Respond to gh issue list with two open issues
        if len(cmd) >= 2 and cmd[0] == "gh" and cmd[1] == "issue" and "list" in cmd:
            R.stdout = json.dumps([{"number": 11}, {"number": 22}])
        return R()

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    rc = commands.cmd_new_goal(tmp_ws.root, src)
    assert rc == 0
    state = GoalState.load(tmp_ws.goal_json)
    assert state.version == 3
    assert state.label == "goal:v3"
    assert state.cycles == 0
    # Check we tried to close prior label issues
    list_calls = [c for c in calls if "list" in c]
    close_calls = [c for c in calls if "close" in c]
    assert any("goal:v2" in c for c in list_calls)
    assert len(close_calls) == 2  # closed #11 and #22

    # Inbox got [OVERRIDE]
    inbox = tmp_ws.state_dir / "inbox" / "lead.md"
    assert inbox.exists()
    body = inbox.read_text()
    assert "[OVERRIDE" in body
    assert "goal:v3" in body

    # AGENTS.md mentions new label
    assert "goal:v3" in (tmp_ws.role_cfg_dir("lead") / "AGENTS.md").read_text()


def test_cmd_new_goal_handles_gh_failure_gracefully(tmp_ws: Workspace, monkeypatch):
    GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x").save(tmp_ws.goal_json)
    src = tmp_ws.root / "NEW_GOAL.md"
    src.write_text("# v2\n")

    def failing_run(cmd, *a, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "gh blew up"
        return R()

    monkeypatch.setattr(commands.subprocess, "run", failing_run)
    rc = commands.cmd_new_goal(tmp_ws.root, src)
    # Should succeed despite gh failures
    assert rc == 0
    assert GoalState.load(tmp_ws.goal_json).version == 2


def test_cmd_new_goal_clears_stopped_and_exit_reason(tmp_ws: Workspace, monkeypatch):
    tmp_ws.stop("goal-complete")
    tmp_ws.exit_reason_file.write_text("goal-complete\n")
    src = tmp_ws.root / "NEW_GOAL.md"
    src.write_text("# new\n")
    monkeypatch.setattr(commands.subprocess, "run", _explode_subprocess)
    rc = commands.cmd_new_goal(tmp_ws.root, src)
    assert rc == 0
    assert not tmp_ws.is_stopped()
    assert not tmp_ws.exit_reason_file.exists()


# ─── helpers ───
class _StubBackend:
    name = "stub"

    def __init__(self, healthy: bool = True):
        self._healthy = healthy
        self.calls: list[dict[str, Any]] = []

    def doctor(self):
        return [] if self._healthy else ["unhealthy"]

    def run_role(self, role, model, config_dir, add_dirs, prompt, log_path, timeout, cwd, first_run):
        self.calls.append({"role": role, "first_run": first_run})
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("ok\n")
        return 0


class _StoppingBackend(_StubBackend):
    """Writes STOPPED sentinel after first lead tick."""

    def __init__(self):
        super().__init__(healthy=True)
        self._ws_root: Path | None = None

    def run_role(self, role, model, config_dir, add_dirs, prompt, log_path, timeout, cwd, first_run):
        rc = super().run_role(role, model, config_dir, add_dirs, prompt, log_path, timeout, cwd, first_run)
        # config_dir is <ws>/cfg/<role>; sentinel is at <ws>/state/STOPPED
        ws_root = config_dir.parent.parent
        (ws_root / "state").mkdir(parents=True, exist_ok=True)
        (ws_root / "state" / "STOPPED").write_text("goal-complete\n")
        return rc


def _explode_subprocess(cmd, *a, **kw):
    raise AssertionError(f"subprocess.run called unexpectedly: {cmd}")
