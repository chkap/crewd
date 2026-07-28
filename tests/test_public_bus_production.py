"""Production-path regressions for the public-bus gate (issue #29).

Prove that a *normal* ``crewd run`` — built via ``commands._build_orchestrator``
with no test injection of the gate — enforces the public issue-bus invariant:
Lead cannot route Worker or finish the goal unless the required GitHub artifacts
exist. The GitHub effect seam ``commands._make_github_client`` is patched with a
deterministic in-memory fake (no network); the enforcement itself is the default
production wiring, not a test-only injection.
"""
from __future__ import annotations

import pytest

from crewd import commands
from crewd.config import CrewConfig, GoalState
from crewd.workspace import Workspace

from fakes import FakeExecutor, dispatch_to, finish, wait
from fake_github import FakeGitHubClient

CREW = "testcrew"
REPO = "acme/widget"
GOAL = "goal:v1"  # tmp_ws initialises goal:v1


@pytest.fixture
def enable_bus(monkeypatch):
    """Re-enable the default-on public bus that the suite disables by default."""
    monkeypatch.delenv("CREWD_DISABLE_PUBLIC_BUS", raising=False)


def _stub_backend():
    class _B:
        def doctor(self):
            return []

    return _B()


def _wire_run(monkeypatch, fake_exec: FakeExecutor, client: FakeGitHubClient) -> None:
    monkeypatch.setattr(commands, "get_backend", lambda _name: _stub_backend())
    monkeypatch.setattr(commands, "build_executor", lambda _cfg: fake_exec)
    monkeypatch.setattr(commands, "_make_github_client", lambda _cfg: client)


def _seed_goal(ws: Workspace) -> None:
    GoalState(version=1, label=GOAL, cycles=0,
              goal_md_sha256="").save(ws.goal_json)


def test_default_run_blocks_worker_without_public_artifacts(tmp_ws: Workspace, monkeypatch, enable_bus):
    """No goal/task record → the default production run refuses to launch Worker
    and halts human-blocked, without a test-injected gate."""
    _seed_goal(tmp_ws)
    client = FakeGitHubClient(REPO)  # empty public record
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    _wire_run(monkeypatch, fake, client)

    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert fake.role_calls == []  # Worker never ran
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


def test_default_run_allows_worker_with_valid_record(tmp_ws: Workspace, monkeypatch, enable_bus):
    """A valid current-goal task + public Lead assignment lets the default run
    launch Worker (references derived from the record, not hard-coded)."""
    _seed_goal(tmp_ws)
    client = FakeGitHubClient(REPO)
    client.add_issue(30, "GOAL: x", labels=(GOAL,))
    client.add_issue(29, "task", labels=("crewd:task", GOAL))
    client.add_comment("issue", 29, f"> **[crewd:lead -> worker]** {CREW}\n\nAssigned.")
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), wait("external")])
    _wire_run(monkeypatch, fake, client)

    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert [r.role for r in fake.role_calls] == ["worker"]


def test_default_run_blocks_finish_without_acceptance(tmp_ws: Workspace, monkeypatch, enable_bus):
    """Lead finish is refused by the default run when the umbrella is not closed
    with a public summary — the goal is not marked complete."""
    _seed_goal(tmp_ws)
    client = FakeGitHubClient(REPO)
    client.add_issue(30, "GOAL: x", labels=(GOAL,))  # open, no summary
    fake = FakeExecutor(lead_script=[finish("done")])
    _wire_run(monkeypatch, fake, client)

    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


def test_default_run_allows_finish_with_acceptance(tmp_ws: Workspace, monkeypatch, enable_bus):
    """A closed final-acceptance umbrella + public summary lets the default run
    finish the goal."""
    _seed_goal(tmp_ws)
    client = FakeGitHubClient(REPO)
    client.add_issue(30, "GOAL: x", state="closed", labels=(GOAL,))
    client.add_comment("issue", 30, f"> **[crewd:lead -> all]** {CREW}\n\nDone.")
    fake = FakeExecutor(lead_script=[finish("accepted")])
    _wire_run(monkeypatch, fake, client)

    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"


def test_disable_env_makes_run_inert(tmp_ws: Workspace, monkeypatch):
    """The documented CREWD_DISABLE_PUBLIC_BUS kill-switch bypasses the gate for
    offline recovery (autouse fixture already sets it)."""
    _seed_goal(tmp_ws)
    # No client patch: if the gate were built it would try real gh. It must not be.
    called = {"n": 0}

    def _boom(_cfg):
        called["n"] += 1
        raise AssertionError("client must not be constructed when disabled")

    monkeypatch.setattr(commands, "_make_github_client", _boom)
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    monkeypatch.setattr(commands, "get_backend", lambda _name: _stub_backend())
    monkeypatch.setattr(commands, "build_executor", lambda _cfg: fake)

    rc = commands.cmd_run(tmp_ws.root, once=False, role=None)
    assert rc == 0
    assert called["n"] == 0
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"
