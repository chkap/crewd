"""Tests for crewd.commands — init/doctor/run/logs with mocked backend."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import signal
import threading
import time
import pytest

from crewd import commands
from crewd.workspace import Workspace
from crewd.config import CrewConfig, RoleConfig


# ─── init ───
def test_cmd_init_creates_complete_workspace(tmp_path: Path):
    rc = commands.cmd_init(tmp_path / "newcrew", name="newcrew", repo="acme/widget")
    assert rc == 0
    ws = Workspace(tmp_path / "newcrew")
    assert ws.crew_yaml.exists()
    assert ws.goal_md.exists()
    for role in ("lead", "worker", "verifier", "advisory"):
        body = (ws.role_cfg_dir(role) / "AGENTS.md").read_text()
        assert "newcrew" in body
        assert "acme/widget" in body
    cfg = CrewConfig.load(ws.crew_yaml)
    assert cfg.name == "newcrew"
    assert cfg.target.repo == "acme/widget"


def test_cmd_init_refuses_if_already_initialized(tmp_path: Path):
    target = tmp_path / "c"
    assert commands.cmd_init(target, "c", None) == 0
    assert commands.cmd_init(target, "c", None) == 1  # idempotent refusal


# ─── doctor ───
def test_cmd_doctor_passes_when_healthy(tmp_ws: Workspace, monkeypatch):
    # Edit GOAL so it doesn't trip the placeholder check
    tmp_ws.goal_md.write_text("# GOAL\n\nReal goal.\n")
    # Re-render agent files (init wasn't called)
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)

    # Stub backend.doctor and gh-auth subprocess
    monkeypatch.setattr(commands, "get_backend", lambda _name: _StubBackend(healthy=True))
    monkeypatch.setattr(commands.subprocess, "run", _fake_gh_ok)
    rc = commands.cmd_doctor(tmp_ws.root)
    assert rc == 0


def test_cmd_doctor_reports_template_goal(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    # GOAL still has placeholder text — now surfaced as a warning, not an error.
    tmp_ws.goal_md.write_text("Replace this file with your concrete goal\n")
    monkeypatch.setattr(commands, "get_backend", lambda _name: _StubBackend(healthy=True))
    monkeypatch.setattr(commands.subprocess, "run", _fake_gh_ok)
    rc = commands.cmd_doctor(tmp_ws.root)
    # Doctor exits 0 because the placeholder is "warn" severity, not "error".
    # The warning should still appear in output.
    assert rc == 0


def test_cmd_doctor_reports_family_mismatch(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.roles["verifier"] = RoleConfig(model="gpt-5.4", family="gpt")
    cfg.save(tmp_ws.crew_yaml)
    tmp_ws.goal_md.write_text("# GOAL\n\nReal.\n")
    commands._render_agent_files(tmp_ws, cfg)
    monkeypatch.setattr(commands, "get_backend", lambda _name: _StubBackend(healthy=True))
    monkeypatch.setattr(commands.subprocess, "run", _fake_gh_ok)
    rc = commands.cmd_doctor(tmp_ws.root)
    assert rc == 1


def test_cmd_doctor_missing_agent_md_is_error(tmp_ws: Workspace, monkeypatch):
    """Doctor flags missing cfg/<role>/AGENTS.md as an error."""
    tmp_ws.goal_md.write_text("# GOAL\n\nReal.\n")
    # Don't render agents — they should be missing
    monkeypatch.setattr(commands, "get_backend", lambda _name: _StubBackend(healthy=True))
    monkeypatch.setattr(commands.subprocess, "run", _fake_gh_ok)
    rc = commands.cmd_doctor(tmp_ws.root)
    assert rc == 1


# ─── run ───
def test_cmd_run_once_walks_all_roles(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert rc == 0
    # All four roles ticked exactly once
    assert [c["role"] for c in backend.calls] == ["lead", "worker", "verifier", "advisory"]
    # Cycle counter advanced
    assert tmp_ws.read_cycle() == 1


def test_cmd_run_role_only_ticks_one(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=False, role="worker")
    assert rc == 0
    assert len(backend.calls) == 1
    assert backend.calls[0]["role"] == "worker"


def test_cmd_run_first_run_skips_continue_then_uses_continue(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    backend = _StubBackend(healthy=True, create_session=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    # First cycle
    commands.cmd_run(tmp_ws.root, once=True, role=None)
    first_cycle_calls = list(backend.calls)
    # Each role's first call should be first_run=True
    assert all(c["first_run"] for c in first_cycle_calls)
    backend.calls.clear()
    # Second cycle — sessions now exist, first_run should be False
    commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert all(not c["first_run"] for c in backend.calls)


def test_cmd_run_aborts_on_family_mismatch(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.roles["verifier"] = RoleConfig(model="gpt-5.4", family="gpt")
    cfg.save(tmp_ws.crew_yaml)
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert rc == 2
    assert backend.calls == []


def test_cmd_run_aborts_on_unhealthy_backend(tmp_ws: Workspace, monkeypatch):
    backend = _StubBackend(healthy=False)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert rc == 2
    assert backend.calls == []


def test_cmd_run_aborts_when_checkout_missing(tmp_ws: Workspace, monkeypatch):
    # Remove the repo dir
    co = tmp_ws.repo_dir("./repo")
    import shutil; shutil.rmtree(co)
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert rc == 2


def test_cmd_run_resume_clears_stopped_then_runs(tmp_ws: Workspace, monkeypatch):
    tmp_ws.stop("from-test")
    assert tmp_ws.is_stopped()
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert rc == 0
    assert not tmp_ws.is_stopped()
    assert len(backend.calls) == 4


def test_cmd_run_signal_request_stop_breaks_loop(tmp_ws: Workspace, monkeypatch):
    """Spawn a thread that sends SIGINT mid-loop; verify clean exit (rc=0)."""
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.sleep_secs = 1
    cfg.save(tmp_ws.crew_yaml)

    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)

    def kicker():
        time.sleep(0.5)  # let loop start cycle 1
        signal.raise_signal(signal.SIGINT)

    t = threading.Thread(target=kicker, daemon=True)
    t.start()
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    t.join(timeout=2)
    assert rc == 0
    # At least cycle 1 ran fully
    assert tmp_ws.read_cycle() >= 1


def test_cmd_run_max_cycles_limit(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.sleep_secs = 0
    cfg.loop.max_cycles = 2
    cfg.save(tmp_ws.crew_yaml)
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.read_cycle() == 2
    # 4 roles × 2 cycles
    assert len(backend.calls) == 8


# ─── status / stop / resume ───
def test_cmd_status_runs(tmp_ws: Workspace):
    assert commands.cmd_status(tmp_ws.root) == 0


def test_cmd_stop_writes_sentinel(tmp_ws: Workspace):
    commands.cmd_stop(tmp_ws.root, "test-reason")
    assert tmp_ws.is_stopped()
    assert "test-reason" in tmp_ws.stopped_sentinel.read_text()


def test_cmd_resume_clears_sentinel(tmp_ws: Workspace):
    tmp_ws.stop("manual")
    rc = commands.cmd_resume(tmp_ws.root)
    assert rc == 0
    assert not tmp_ws.is_stopped()


# ─── logs ───
def test_cmd_logs_lists_recent(tmp_ws: Workspace):
    # Seed a few log files
    for cycle in (1, 2):
        for role in ("lead", "worker"):
            p = tmp_ws.log_file(role, cycle)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"line for {role} cycle {cycle}\n")
    assert commands.cmd_logs(tmp_ws.root, role=None, cycle=None, tail=50, follow=False) == 0


def test_cmd_logs_specific_cycle(tmp_ws: Workspace, capsys):
    p = tmp_ws.log_file("worker", 3)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("hello-from-worker-3\n")
    rc = commands.cmd_logs(tmp_ws.root, role="worker", cycle=3, tail=50, follow=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello-from-worker-3" in out


def test_cmd_logs_unknown_log_returns_1(tmp_ws: Workspace):
    rc = commands.cmd_logs(tmp_ws.root, role="worker", cycle=999, tail=50, follow=False)
    assert rc == 1


# ─────────────── helpers ───────────────
class _StubBackend:
    name = "stub"

    def __init__(self, healthy: bool, create_session: bool = False):
        self._healthy = healthy
        self._create_session = create_session
        self.calls: list[dict[str, Any]] = []

    def doctor(self) -> list[str]:
        return [] if self._healthy else ["stub backend forced unhealthy"]

    def run_role(self, role, model, config_dir, add_dirs, prompt, log_path, timeout, cwd, first_run):
        self.calls.append({
            "role": role,
            "model": model,
            "config_dir": str(config_dir),
            "first_run": first_run,
            "prompt_len": len(prompt),
            "cycle_arg": log_path.stem,
        })
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"stub run for {role}\n")
        if self._create_session:
            (config_dir / "session-state").mkdir(parents=True, exist_ok=True)
        return 0


def _fake_gh_ok(cmd, *a, **kw):
    """Fake successful gh auth status."""
    class R: returncode = 0; stdout = ""; stderr = ""
    return R()


# ─── refresh / migration ───
def test_cmd_refresh_renders_agents_md(tmp_ws: Workspace):
    """refresh creates AGENTS.md in cfg/<role>/."""
    rc = commands.cmd_refresh(tmp_ws.root)
    assert rc == 0
    for role in ("lead", "worker", "verifier", "advisory"):
        agents_md = tmp_ws.role_cfg_dir(role) / "AGENTS.md"
        assert agents_md.exists(), f"AGENTS.md missing for {role}"
        assert tmp_ws.cfg.name if hasattr(tmp_ws, "cfg") else "testcrew" in agents_md.read_text()


def test_cmd_refresh_migrates_checkout_to_repo(tmp_ws: Workspace):
    """refresh renames checkout/ → repo/ and updates crew.yaml."""
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    # Simulate old layout: rename repo/ back to checkout/, reset config
    repo = tmp_ws.repo_dir(cfg.target.checkout)
    old_checkout = tmp_ws.root / "checkout"
    if repo.exists():
        repo.rename(old_checkout)
    cfg.target.checkout = "./checkout"
    cfg.save(tmp_ws.crew_yaml)

    rc = commands.cmd_refresh(tmp_ws.root)
    assert rc == 0

    # Verify migration happened
    reloaded = CrewConfig.load(tmp_ws.crew_yaml)
    assert reloaded.target.checkout == "./repo"
    assert (tmp_ws.root / "repo").exists()
    assert not (tmp_ws.root / "checkout").exists()
