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
    assert cfg.backend == "copilot"


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
        add_dirs=[cfg_dir],  # only the working dir → no unmapped-dir diagnostic
        prompt="do one tick",
        log_path=log,
        timeout=60,
        cwd=cfg_dir,
        first_run=True,
    )

    assert rc == 0
    body = log.read_text()
    assert "backend=copilot-sdk" in body
    assert "outcome=idle_completed" in body
    sidecar = log.with_suffix(log.suffix + ".attempt.json")
    assert sidecar.exists()
    import json

    data = json.loads(sidecar.read_text())
    assert data["outcome"] == "idle_completed"
    # SDK-init marker persisted so the next tick resumes deliberately.
    assert (cfg_dir / ".crewd-sdk-session").exists()


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
        add_dirs=[cfg_dir],
        prompt="p",
        cwd=cfg_dir,
    )
    backend.run_role(log_path=tmp_path / "a.log", timeout=10, first_run=True, **common)
    assert seen["resume"] is False
    backend.run_role(log_path=tmp_path / "b.log", timeout=10, first_run=False, **common)
    assert seen["resume"] is True


def test_sdk_backend_surfaces_unmapped_add_dirs(tmp_path: Path):
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

    cfg_dir = tmp_path / "cfg" / "worker"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    log = tmp_path / "worker.log"
    backend = SdkBackend(ops_factory=lambda **kw: _FakeOps(**kw))
    backend.run_role(
        role="worker",
        model="m",
        config_dir=cfg_dir,
        add_dirs=[cfg_dir, worktree],  # worktree is unmappable under the SDK
        prompt="p",
        log_path=log,
        timeout=10,
        cwd=cfg_dir,
        first_run=True,
    )
    body = log.read_text()
    assert "cannot mount extra add-dirs" in body
    assert str(worktree) in body


def test_sdk_backend_tainted_session_recovers_fresh(tmp_path: Path):
    from crewd.backends import SdkBackend
    from crewd.session_backend import RunSignal, TaintStore, build_session_id

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

    cfg_dir = tmp_path / "cfg" / "worker"
    cfg_dir.mkdir(parents=True)
    # Simulate a prior tick that left a resumable marker, then a taint.
    sid = build_session_id(workspace_id=str(cfg_dir.resolve()), goal_label="", role="worker")
    (cfg_dir / ".crewd-sdk-session").write_text(sid)
    TaintStore(cfg_dir / ".crewd-sdk-taint").taint(sid)

    backend = SdkBackend(ops_factory=lambda **kw: _FakeOps(**kw))
    log = tmp_path / "worker.log"
    rc = backend.run_role(
        role="worker",
        model="m",
        config_dir=cfg_dir,
        add_dirs=[cfg_dir],
        prompt="p",
        log_path=log,
        timeout=10,
        cwd=cfg_dir,
        first_run=False,
    )
    assert rc == 0
    # A tainted resumable session must be recovered by a FRESH create, not resume.
    assert calls["resume"] == [False]
    assert "is tainted; starting a fresh session" in log.read_text()


def test_get_backend_unknown_still_raises():
    from crewd.backends import get_backend

    with pytest.raises(ValueError):
        get_backend("nope")
