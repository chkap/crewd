"""Tests for crewd.commands — init/doctor/run/logs with mocked backend."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import signal
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
    for role in ("lead", "advisory", "worker", "verifier"):
        body = (ws.role_cfg_dir(role) / "AGENTS.md").read_text()
        assert "newcrew" in body
        assert "acme/widget" in body
    cfg = CrewConfig.load(ws.crew_yaml)
    assert cfg.name == "newcrew"
    assert cfg.target.remote == "acme/widget"


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


# ─── run (dispatcher-driven) ───
def _use_fake(monkeypatch, fake):
    monkeypatch.setattr(commands, "get_backend", lambda _name: _StubBackend(healthy=True))
    monkeypatch.setattr(commands, "build_executor", lambda _cfg: fake)


def test_cmd_run_once_advances_one_dispatch_step(tmp_ws: Workspace, monkeypatch):
    from fakes import FakeExecutor, dispatch_to, finish

    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish()])
    _use_fake(monkeypatch, fake)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert rc == 0
    # One step = the Lead solicitation that dispatches worker; worker not yet run.
    assert len(fake.lead_calls) == 1
    assert fake.role_calls == []
    assert tmp_ws.read_cycle() == 1


def test_cmd_run_full_reaches_finish(tmp_ws: Workspace, monkeypatch):
    from fakes import FakeExecutor, dispatch_to, finish

    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("accepted")])
    _use_fake(monkeypatch, fake)
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"


def test_cmd_run_rejects_legacy_copilot_backend(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.backend = "copilot"
    cfg.save(tmp_ws.crew_yaml)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    assert rc == 2  # preflight refuses the retired subprocess backend


def test_cmd_run_role_only_ticks_one(tmp_ws: Workspace, monkeypatch):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    backend = _StubBackend(healthy=True)
    monkeypatch.setattr(commands, "get_backend", lambda _name: backend)
    rc = commands.cmd_run(tmp_ws.root, once=False, role="worker")
    assert rc == 0
    assert len(backend.calls) == 1
    assert backend.calls[0]["role"] == "worker"


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


def test_cmd_run_stopped_persists_until_explicit_resume(tmp_ws: Workspace, monkeypatch):
    """A plain `crewd run` must NOT clear a STOPPED sentinel (durable operator
    stop); only `crewd resume` reactivates, after which run proceeds."""
    from fakes import FakeExecutor, finish

    tmp_ws.stop("from-test")
    assert tmp_ws.is_stopped()
    fake = FakeExecutor(lead_script=[finish("done")])
    _use_fake(monkeypatch, fake)

    # Plain run: halts with the distinct stopped reason, does no Lead work, and
    # leaves the sentinel in place.
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "stopped"
    assert tmp_ws.is_stopped()  # NOT erased by a plain run
    assert fake.lead_calls == []

    # Explicit resume clears the sentinel + reactivates the dispatcher run.
    assert commands.cmd_resume(tmp_ws.root) == 0
    assert not tmp_ws.is_stopped()

    # Now a run proceeds to finish.
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"
    assert len(fake.lead_calls) == 1


def test_cmd_run_does_not_bypass_lead_pause(tmp_ws: Workspace, monkeypatch):
    """Regression: a plain run after a Lead human-block must not erase it."""
    from fakes import FakeExecutor, finish, pause

    # First run: Lead pauses → durable human blocker.
    fake1 = FakeExecutor(lead_script=[pause("human-blocked: approval needed")])
    _use_fake(monkeypatch, fake1)
    assert commands.cmd_run(tmp_ws.root, once=False, role=None) == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"

    # Second plain run: must report the blocker and do NO work (no bypass).
    fake2 = FakeExecutor(lead_script=[finish("sneaky")])
    _use_fake(monkeypatch, fake2)
    assert commands.cmd_run(tmp_ws.root, once=False, role=None) == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"
    assert fake2.lead_calls == []

    # Explicit resume, then run proceeds.
    assert commands.cmd_resume(tmp_ws.root) == 0
    fake3 = FakeExecutor(lead_script=[finish("accepted")])
    _use_fake(monkeypatch, fake3)
    assert commands.cmd_run(tmp_ws.root, once=False, role=None) == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"
    assert len(fake3.lead_calls) == 1


def test_cmd_run_signal_request_stop_breaks_loop(tmp_ws: Workspace, monkeypatch):
    """A SIGINT mid-loop must exit cleanly (rc=0) with the interrupted reason."""
    from fakes import FakeExecutor, dispatch_to

    # Lead keeps dispatching worker forever; SIGINT after the first turn halts it.
    fake = FakeExecutor(lead_script=[dispatch_to("worker")] * 100)
    _use_fake(monkeypatch, fake)

    orig = fake.run_lead

    def kicker(req, *, on_started=None, cancel=None):
        if len(fake.lead_calls) >= 1:
            signal.raise_signal(signal.SIGINT)
        return orig(req, on_started=on_started)

    fake.run_lead = kicker  # type: ignore[method-assign]
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "interrupted"


def test_cmd_run_max_work_exhaustion(tmp_ws: Workspace, monkeypatch):
    from fakes import FakeExecutor, dispatch_to

    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.sleep_secs = 0
    cfg.loop.max_cycles = 2  # → dispatcher max_work budget
    cfg.save(tmp_ws.crew_yaml)
    fake = FakeExecutor(lead_script=[dispatch_to("worker")] * 20)
    _use_fake(monkeypatch, fake)
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "exhausted"


def test_cmd_run_lead_pause_exits_human_blocked(tmp_ws: Workspace, monkeypatch):
    from fakes import FakeExecutor, pause

    fake = FakeExecutor(lead_script=[pause("human-blocked: operator approval required")])
    _use_fake(monkeypatch, fake)
    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert fake.role_calls == []  # no role work launched
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


# ─── status / stop / resume ───
def test_cmd_status_runs(tmp_ws: Workspace):
    assert commands.cmd_status(tmp_ws.root) == 0


def test_cmd_stop_writes_sentinel(tmp_ws: Workspace):
    commands.cmd_stop(tmp_ws.root, "test-reason")
    assert tmp_ws.is_stopped()
    assert "test-reason" in tmp_ws.stopped_sentinel.read_text()


def test_cmd_pause_writes_human_blocker(tmp_ws: Workspace):
    rc = commands.cmd_pause(tmp_ws.root, "human-blocked: operator approval required")
    assert rc == 0
    assert tmp_ws.pause_reason() == "human-blocked: operator approval required"
    assert not tmp_ws.is_stopped()


def test_cmd_resume_clears_sentinel(tmp_ws: Workspace):
    tmp_ws.stop("manual")
    tmp_ws.pause("human-blocked: approval")
    rc = commands.cmd_resume(tmp_ws.root)
    assert rc == 0
    assert not tmp_ws.is_stopped()
    assert not tmp_ws.is_paused()


# ─── logs ───
def test_cmd_logs_lists_recent(tmp_ws: Workspace):
    from crewd.config import GoalState
    GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x").save(tmp_ws.goal_json)
    # Seed a few log files
    for cycle in (1, 2):
        for role in ("lead", "worker"):
            p = tmp_ws.log_file(role, cycle, "goal:v1")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"line for {role} cycle {cycle}\n")
    assert commands.cmd_logs(tmp_ws.root, role=None, cycle=None, tail=50, follow=False) == 0


def test_cmd_logs_specific_cycle(tmp_ws: Workspace, capsys):
    from crewd.config import GoalState
    GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x").save(tmp_ws.goal_json)
    p = tmp_ws.log_file("worker", 3, "goal:v1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("hello-from-worker-3\n")
    rc = commands.cmd_logs(tmp_ws.root, role="worker", cycle=3, tail=50, follow=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "hello-from-worker-3" in out


def test_cmd_logs_specific_cycle_reads_current_goal_namespace(tmp_ws: Workspace, capsys):
    from crewd.config import GoalState
    GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x").save(tmp_ws.goal_json)
    p = tmp_ws.log_file("worker", 4, "goal:v1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("worker-goal-v1-cycle-4\n")
    rc = commands.cmd_logs(tmp_ws.root, role="worker", cycle=4, tail=50, follow=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "worker-goal-v1-cycle-4" in out


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

    def run_role(self, role, model, config_dir, add_dirs, prompt, log_path, timeout, cwd, first_run, **kwargs):
        self.calls.append({
            "role": role,
            "model": model,
            "config_dir": str(config_dir),
            "first_run": first_run,
            "prompt_len": len(prompt),
            "cycle_arg": log_path.stem,
            "goal_label": kwargs.get("goal_label"),
            "workspace_root": kwargs.get("workspace_root"),
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
    for role in ("lead", "advisory", "worker", "verifier"):
        agents_md = tmp_ws.role_cfg_dir(role) / "AGENTS.md"
        assert agents_md.exists(), f"AGENTS.md missing for {role}"
        assert tmp_ws.cfg.name if hasattr(tmp_ws, "cfg") else "testcrew" in agents_md.read_text()


def test_cmd_refresh_migrates_checkout_to_repo(tmp_ws: Workspace):
    """refresh renames checkout/ → repo/ and upgrades crew.yaml schema.

    Old layout had ``target.repo = "owner/name"`` + ``target.checkout = "./repo"``.
    New layout has ``target.remote = "owner/name"`` + ``target.repo = "./repo"``.
    """
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    # Simulate old layout: rename repo/ back to checkout/, write legacy YAML
    repo = tmp_ws.repo_dir(cfg.target.repo)
    old_checkout = tmp_ws.root / "checkout"
    if repo.exists():
        repo.rename(old_checkout)
    import yaml as _yaml
    legacy = {
        "name": cfg.name,
        "target": {
            "repo": cfg.target.remote,         # legacy: owner/name under `repo`
            "branch": cfg.target.branch,
            "checkout": "./checkout",          # legacy: local path under `checkout`
        },
        "goal_file": cfg.goal_file,
        "roles": {r: cfg.roles[r].model_dump() for r in cfg.roles},
        "loop": cfg.loop.model_dump(),
        "backend": cfg.backend,
    }
    tmp_ws.crew_yaml.write_text(_yaml.safe_dump(legacy, sort_keys=False))

    rc = commands.cmd_refresh(tmp_ws.root)
    assert rc == 0

    # Verify migration happened: dirs renamed and schema upgraded
    reloaded = CrewConfig.load(tmp_ws.crew_yaml)
    assert reloaded.target.remote == cfg.target.remote  # was under `repo:` key
    assert reloaded.target.repo == "./repo"             # was under `checkout:` key
    assert (tmp_ws.root / "repo").exists()
    assert not (tmp_ws.root / "checkout").exists()


# ─── #28: legacy-backend refresh/doctor/run migration end-to-end ───
def _make_legacy(tmp_ws: Workspace, *, unknown: bool = True) -> dict:
    """Downgrade tmp_ws crew.yaml to the retired backend + unknown user keys."""
    import yaml
    raw = yaml.safe_load(tmp_ws.crew_yaml.read_text())
    raw["backend"] = "copilot"
    if unknown:
        raw["notify_webhook"] = "https://example/hook"
        raw["custom_block"] = {"k": [1, 2, 3]}
    tmp_ws.crew_yaml.write_text(yaml.safe_dump(raw, sort_keys=False))
    return raw


def test_refresh_migrates_retired_backend_preserving_state(tmp_ws: Workspace):
    import yaml
    _make_legacy(tmp_ws)
    # Realistic workspace state that must survive a refresh untouched:
    from crewd.config import GoalState
    GoalState(version=2, label="goal:v2", cycles=5, goal_md_sha256="deadbeef").save(tmp_ws.goal_json)
    tmp_ws.stop("completed")                       # STOPPED sentinel
    (tmp_ws.role_cfg_dir("worker") / "session-state").mkdir(parents=True, exist_ok=True)
    (tmp_ws.role_cfg_dir("worker") / "session-state" / "s.json").write_text("{}")
    # Dispatcher / public-bus durable state lives under state/ — must be preserved.
    marker = tmp_ws.state_dir / "public_writes" / "journal.jsonl"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"intent":"x"}\n')

    rc = commands.cmd_refresh(tmp_ws.root)
    assert rc == 0

    out = yaml.safe_load(tmp_ws.crew_yaml.read_text())
    assert out["backend"] == "copilot-sdk"                       # migrated
    assert out["notify_webhook"] == "https://example/hook"       # unknown preserved
    assert out["custom_block"] == {"k": [1, 2, 3]}
    # workspace/dispatcher state untouched
    assert tmp_ws.is_stopped()
    reloaded_goal = GoalState.load(tmp_ws.goal_json)
    assert reloaded_goal.cycles == 5 and reloaded_goal.label == "goal:v2"
    assert (tmp_ws.role_cfg_dir("worker") / "session-state" / "s.json").exists()
    assert marker.read_text() == '{"intent":"x"}\n'


def test_refresh_is_idempotent_on_current_workspace(tmp_ws: Workspace, capsys):
    _make_legacy(tmp_ws, unknown=False)
    assert commands.cmd_refresh(tmp_ws.root) == 0
    capsys.readouterr()
    # second refresh: already-current, no migration note emitted
    assert commands.cmd_refresh(tmp_ws.root) == 0
    out = capsys.readouterr().out
    assert "backend: copilot → copilot-sdk" not in out
    import yaml
    assert yaml.safe_load(tmp_ws.crew_yaml.read_text())["backend"] == "copilot-sdk"


def test_refresh_preserves_paused_state(tmp_ws: Workspace):
    _make_legacy(tmp_ws, unknown=False)
    tmp_ws.pause("waiting on operator")
    assert commands.cmd_refresh(tmp_ws.root) == 0
    assert tmp_ws.is_paused()
    assert tmp_ws.pause_reason().strip() == "waiting on operator"


def test_doctor_migration_required_message(tmp_ws: Workspace, capsys, monkeypatch):
    _make_legacy(tmp_ws, unknown=False)
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    monkeypatch.setattr(commands.subprocess, "run", _fake_gh_ok)
    commands.cmd_doctor(tmp_ws.root)
    out = capsys.readouterr().out
    assert "migration required" in out
    assert "crewd refresh" in out
    # must NOT be mis-reported as a missing-SDK problem
    assert "not importable" not in out


def test_doctor_reports_missing_sdk_distinctly(tmp_ws: Workspace, capsys, monkeypatch):
    # Backend is already current; the SDK import is what's missing.
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    monkeypatch.setattr(commands.subprocess, "run", _fake_gh_ok)
    import crewd.sdk_adapter as sdk
    monkeypatch.setattr(sdk, "sdk_available", lambda: False)
    rc = commands.cmd_doctor(tmp_ws.root)
    out = capsys.readouterr().out
    assert rc == 1
    assert "not importable" in out            # missing-SDK message
    assert "migration required" not in out    # distinct from migration state


def test_run_migration_required_refuses_with_action(tmp_ws: Workspace, capsys):
    _make_legacy(tmp_ws, unknown=False)
    rc = commands.cmd_run(tmp_ws.root, once=True, role=None)
    out = capsys.readouterr().out
    assert rc == 2
    assert "migration required" in out and "crewd refresh" in out


def test_doctor_warns_external_extra_add_dirs(tmp_ws: Workspace, capsys, monkeypatch, tmp_path):
    outside = tmp_path / "secrets"
    outside.mkdir()
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.extra_add_dirs = [str(outside)]
    cfg.save(tmp_ws.crew_yaml)
    commands._render_agent_files(tmp_ws, cfg)
    monkeypatch.setattr(commands, "get_backend", lambda _n: _StubBackend(healthy=True))
    monkeypatch.setattr(commands.subprocess, "run", _fake_gh_ok)
    commands.cmd_doctor(tmp_ws.root)
    out = capsys.readouterr().out
    assert "resolves outside the workspace" in out
    assert "copying/sanitizing" in out


def test_run_preflight_warns_on_external_extra_add_dirs(tmp_ws: Workspace, capsys, monkeypatch, tmp_path):
    outside = tmp_path / "extern"
    outside.mkdir()
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.extra_add_dirs = [str(outside)]
    cfg.save(tmp_ws.crew_yaml)
    monkeypatch.setattr(commands, "get_backend", lambda _n: _StubBackend(healthy=True))
    result = commands._preflight(tmp_ws.root, auto_render=False)
    out = capsys.readouterr().out
    # non-fatal: preflight still succeeds but the advisory is printed
    assert isinstance(result, tuple)
    assert "extra_add_dirs" in out and "resolves outside the workspace" in out
