"""Tests for crewd.config — schema, save/load roundtrip, family validation."""
from __future__ import annotations
from pathlib import Path
import pytest

from crewd.config import CrewConfig, RoleConfig, default_config


def test_default_config_has_four_roles():
    cfg = default_config("demo", "acme/widget")
    assert set(cfg.roles) == {"lead", "worker", "verifier", "advisory"}
    assert cfg.target.remote == "acme/widget"
    assert cfg.target.repo == "./repo"
    assert cfg.target.branch == "main"
    assert cfg.backend == "copilot-sdk"


def test_family_validation_passes_by_default():
    cfg = default_config("demo")
    assert cfg.validate_families() == []


def test_family_validation_catches_same_family():
    cfg = default_config("demo")
    cfg.roles["verifier"] = RoleConfig(model="gpt-5.4", family="gpt")  # same as worker
    errs = cfg.validate_families()
    assert len(errs) == 1
    assert "rubber stamp" in errs[0]


def test_family_validation_skips_when_role_missing():
    cfg = default_config("demo")
    del cfg.roles["verifier"]
    assert cfg.validate_families() == []


def test_save_load_roundtrip(tmp_path: Path):
    cfg = default_config("demo", "acme/widget")
    cfg.loop.sleep_secs = 30
    p = tmp_path / "crew.yaml"
    cfg.save(p)
    loaded = CrewConfig.load(p)
    assert loaded.name == "demo"
    assert loaded.target.remote == "acme/widget"
    assert loaded.target.repo == "./repo"
    assert loaded.loop.sleep_secs == 30
    assert loaded.roles["worker"].family == "gpt"


def test_backend_accepts_copilot_sdk_literal(tmp_path: Path):
    cfg = default_config("demo")
    cfg.backend = "copilot-sdk"
    p = tmp_path / "crew.yaml"
    cfg.save(p)
    assert CrewConfig.load(p).backend == "copilot-sdk"


def test_get_backend_returns_sdk_backend():
    from crewd.backends import SdkBackend, get_backend

    b = get_backend("copilot-sdk")
    assert isinstance(b, SdkBackend)
    assert b.name == "copilot-sdk"
    # doctor() must run without importing/launching a runtime.
    assert isinstance(b.doctor(), list)


def test_sdk_backend_run_role_is_operational_with_injected_fake(tmp_path: Path):
    """The selectable backend path must actually execute a role attempt.

    Uses an injected fake SdkOps so the real command path is exercised without
    the SDK: no NotImplementedError, a clean tick returns 0, and the typed
    lifecycle sidecar is persisted for #11.
    """
    from crewd.backends import SdkBackend
    from crewd.session_backend import RunSignal

    class _FakeOps:
        def __init__(self, **kw):
            self.session_id = kw["session_id"]
            self.role = kw["role"]
            self.opened_resume = None

        def open(self, *, resume):
            self.opened_resume = resume

        def run(self, prompt, timeout):
            return RunSignal.IDLE

        def abort(self, timeout):  # pragma: no cover - not hit on happy path
            return True

        def drain_events(self):
            return ["event:assistant_message"]

        def disconnect(self):
            pass

        def force_stop(self):  # pragma: no cover
            pass

    cfg_dir = tmp_path / "cfg" / "worker"
    log = tmp_path / "logs" / "worker.log"
    backend = SdkBackend(ops_factory=lambda **kw: _FakeOps(**kw))

    rc = backend.run_role(
        role="worker",
        model="claude-opus-4.8",
        config_dir=cfg_dir,
        add_dirs=[tmp_path, cfg_dir],  # all under the workspace root
        prompt="do one tick",
        log_path=log,
        timeout=60,
        cwd=cfg_dir,
        first_run=True,
        goal_label="goal:v1",
        workspace_root=tmp_path,
    )

    assert rc == 0
    body = log.read_text()
    assert "backend=copilot-sdk" in body
    assert "outcome=idle_completed" in body
    assert "goal=goal:v1" in body
    sidecar = log.with_suffix(log.suffix + ".attempt.json")
    assert sidecar.exists()
    import json

    data = json.loads(sidecar.read_text())
    assert data["outcome"] == "idle_completed"
    assert data["goal_label"] == "goal:v1"
    # Registry persisted so the next tick resumes deliberately.
    assert (cfg_dir / ".crewd-sdk-sessions.json").exists()


def test_sdk_backend_second_tick_resumes(tmp_path: Path):
    from crewd.backends import SdkBackend
    from crewd.session_backend import RunSignal

    seen = {}

    class _FakeOps:
        def __init__(self, **kw):
            self.session_id = kw["session_id"]
            self.role = kw["role"]

        def open(self, *, resume):
            seen["resume"] = resume

        def run(self, prompt, timeout):
            return RunSignal.IDLE

        def abort(self, timeout):  # pragma: no cover
            return True

        def drain_events(self):
            return []

        def disconnect(self):
            pass

        def force_stop(self):  # pragma: no cover
            pass

    cfg_dir = tmp_path / "cfg" / "worker"
    backend = SdkBackend(ops_factory=lambda **kw: _FakeOps(**kw))
    common = dict(
        role="worker",
        model="m",
        config_dir=cfg_dir,
        add_dirs=[tmp_path, cfg_dir],
        prompt="p",
        cwd=cfg_dir,
        goal_label="goal:v1",
        workspace_root=tmp_path,
    )
    backend.run_role(log_path=tmp_path / "a.log", timeout=10, first_run=True, **common)
    assert seen["resume"] is False
    backend.run_role(log_path=tmp_path / "b.log", timeout=10, first_run=False, **common)
    assert seen["resume"] is True


def test_sdk_backend_new_goal_epoch_creates_fresh_not_resume(tmp_path: Path):
    """A new goal epoch must NOT resume the prior epoch's SDK session."""
    from crewd.backends import SdkBackend
    from crewd.session_backend import RunSignal

    opened = []

    class _FakeOps:
        def __init__(self, **kw):
            self.session_id = kw["session_id"]
            self.role = kw["role"]

        def open(self, *, resume):
            opened.append((self.session_id, resume))

        def run(self, prompt, timeout):
            return RunSignal.IDLE

        def abort(self, timeout):  # pragma: no cover
            return True

        def drain_events(self):
            return []

        def disconnect(self):
            pass

        def force_stop(self):  # pragma: no cover
            pass

    cfg_dir = tmp_path / "cfg" / "worker"
    backend = SdkBackend(ops_factory=lambda **kw: _FakeOps(**kw))
    common = dict(
        role="worker",
        model="m",
        config_dir=cfg_dir,
        add_dirs=[tmp_path, cfg_dir],
        prompt="p",
        cwd=cfg_dir,
        workspace_root=tmp_path,
    )
    # v1 tick, then resume within v1, then a NEW goal epoch v2.
    backend.run_role(log_path=tmp_path / "1.log", timeout=10, first_run=True,
                     goal_label="goal:v1", **common)
    backend.run_role(log_path=tmp_path / "2.log", timeout=10, first_run=False,
                     goal_label="goal:v1", **common)
    backend.run_role(log_path=tmp_path / "3.log", timeout=10, first_run=False,
                     goal_label="goal:v2", **common)

    sid_v1_a, resume_v1_a = opened[0]
    sid_v1_b, resume_v1_b = opened[1]
    sid_v2, resume_v2 = opened[2]
    assert resume_v1_a is False           # first v1 tick creates
    assert resume_v1_b is True            # second v1 tick resumes same session
    assert sid_v1_b == sid_v1_a
    assert resume_v2 is False             # new epoch → fresh create, not resume
    assert sid_v2 != sid_v1_a             # and a different session id


def test_sdk_backend_fails_closed_on_unmountable_path(tmp_path: Path):
    """A required/configured path outside the workspace root must abort the tick
    BEFORE the SDK session is opened (no buried-warning-then-run)."""
    from crewd.backends import SdkBackend
    from crewd.session_backend import RunSignal

    opened = {"count": 0}

    class _FakeOps:
        def __init__(self, **kw):
            self.session_id = kw["session_id"]
            self.role = kw["role"]

        def open(self, *, resume):  # pragma: no cover - must NOT be reached
            opened["count"] += 1

        def run(self, prompt, timeout):  # pragma: no cover
            return RunSignal.IDLE

        def abort(self, timeout):  # pragma: no cover
            return True

        def drain_events(self):  # pragma: no cover
            return []

        def disconnect(self):  # pragma: no cover
            pass

        def force_stop(self):  # pragma: no cover
            pass

    ws = tmp_path / "ws"
    cfg_dir = ws / "cfg" / "worker"
    outside = tmp_path / "outside-data"
    outside.mkdir(parents=True)
    log = tmp_path / "worker.log"
    backend = SdkBackend(ops_factory=lambda **kw: _FakeOps(**kw))
    rc = backend.run_role(
        role="worker",
        model="m",
        config_dir=cfg_dir,
        add_dirs=[ws, outside],  # `outside` is not under the workspace root
        prompt="p",
        log_path=log,
        timeout=10,
        cwd=cfg_dir,
        first_run=True,
        goal_label="goal:v1",
        workspace_root=ws,
    )
    assert rc == 1
    assert opened["count"] == 0           # session was never opened
    body = log.read_text()
    assert "PRE-EXECUTION ERROR" in body
    assert str(outside.resolve()) in body


def test_sdk_backend_default_required_paths_mount_ok(tmp_path: Path):
    """The default workspace layout (root + role config + worktree) mounts."""
    from crewd.backends import SdkBackend
    from crewd.session_backend import RunSignal

    class _FakeOps:
        def __init__(self, **kw):
            self.session_id = kw["session_id"]
            self.role = kw["role"]

        def open(self, *, resume):
            pass

        def run(self, prompt, timeout):
            return RunSignal.IDLE

        def abort(self, timeout):  # pragma: no cover
            return True

        def drain_events(self):
            return []

        def disconnect(self):
            pass

        def force_stop(self):  # pragma: no cover
            pass

    ws = tmp_path / "ws"
    cfg_dir = ws / "cfg" / "worker"
    worktree = cfg_dir / "worktree"
    worktree.mkdir(parents=True)
    log = tmp_path / "worker.log"
    backend = SdkBackend(ops_factory=lambda **kw: _FakeOps(**kw))
    rc = backend.run_role(
        role="worker",
        model="m",
        config_dir=cfg_dir,
        add_dirs=[ws, worktree],  # all under the workspace root
        prompt="p",
        log_path=log,
        timeout=10,
        cwd=cfg_dir,
        first_run=True,
        goal_label="goal:v1",
        workspace_root=ws,
    )
    assert rc == 0
    assert f"working_dir={ws.resolve()}" in log.read_text()


def test_sdk_backend_tainted_session_recovers_fresh_generation(tmp_path: Path):
    from crewd.backends import SdkBackend
    from crewd.session_backend import RunSignal, SessionRegistry, TaintStore

    calls = {"resume": []}

    class _FakeOps:
        def __init__(self, **kw):
            self.session_id = kw["session_id"]
            self.role = kw["role"]

        def open(self, *, resume):
            calls["resume"].append(resume)

        def run(self, prompt, timeout):
            return RunSignal.IDLE

        def abort(self, timeout):  # pragma: no cover
            return True

        def drain_events(self):
            return []

        def disconnect(self):
            pass

        def force_stop(self):  # pragma: no cover
            pass

    ws = tmp_path / "ws"
    cfg_dir = ws / "cfg" / "worker"
    cfg_dir.mkdir(parents=True)
    backend = SdkBackend(ops_factory=lambda **kw: _FakeOps(**kw))
    common = dict(
        role="worker", model="m", config_dir=cfg_dir, add_dirs=[ws],
        prompt="p", cwd=cfg_dir, goal_label="goal:v1", workspace_root=ws,
    )
    # First tick creates + persists the active session id (gen 0).
    backend.run_role(log_path=tmp_path / "1.log", timeout=10, first_run=True, **common)
    # Simulate a force-stop taint of that active session id.
    reg = SessionRegistry(cfg_dir / ".crewd-sdk-sessions.json", workspace_id=str(ws.resolve()))
    active = reg.decide(goal_label="goal:v1", role="worker",
                        taint_store=TaintStore(cfg_dir / ".crewd-sdk-taint"))
    TaintStore(cfg_dir / ".crewd-sdk-taint").taint(active.session_id)

    log = tmp_path / "2.log"
    rc = backend.run_role(log_path=log, timeout=10, first_run=False, **common)
    assert rc == 0
    # Never resumed a tainted id — both ticks created fresh (2nd is a new gen).
    assert calls["resume"] == [False, False]
    assert "recovery generation" in log.read_text()


def test_get_backend_unknown_still_raises():
    from crewd.backends import get_backend

    with pytest.raises(ValueError):
        get_backend("nope")
