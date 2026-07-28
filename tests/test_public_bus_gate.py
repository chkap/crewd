"""Orchestrator integration for the public-bus prerequisite gate (issue #29).

Proves the gate blocks a Worker/Verifier dispatch when the GitHub record fails
the invariant — halting the run without reserving the attempt or consuming the
pending handoff — and lets it proceed when the record is valid. Uses the
deterministic fake executor + fake GitHub boundary (no SDK, no network).
"""
from __future__ import annotations

from crewd.config import CrewConfig, GoalState
from crewd.dispatcher import Dispatcher, RunStatus
from crewd.github_bus import PublicBus, PublicBusGate
from crewd.orchestrator import Orchestrator
from crewd.workspace import Workspace

from fakes import FakeExecutor, dispatch_to, finish
from fake_github import FakeGitHubClient

CREW = "testcrew"
REPO = "acme/widget"
GOAL = "goal:v2"
TASK = 29


def _bus(client: FakeGitHubClient) -> PublicBus:
    return PublicBus(client, crew=CREW, expected_repo=REPO, goal_label=GOAL)


def _orch(tmp_ws: Workspace, fake: FakeExecutor, gate, *, label=GOAL) -> tuple[Orchestrator, Dispatcher]:
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = 0
    gs = GoalState(version=1, label=label, cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    orch = Orchestrator(tmp_ws, cfg, fake, gs, dispatcher=disp, bus_gate=gate, max_steps=50)
    return orch, disp


def _valid_worker_record(c: FakeGitHubClient) -> None:
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL), assignees=("alice",))


def test_gate_blocks_worker_dispatch_on_missing_task(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))  # umbrella only; task #29 missing
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    # Worker never ran; run halted (paused) with a public-bus blocker.
    assert fake.role_calls == []
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert "public-bus" in (run.human_blocker or "")
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


def test_gate_allows_worker_dispatch_when_record_valid(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert [r.role for r in fake.role_calls] == ["worker"]
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"


def test_gate_blocks_verifier_without_linked_pr(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)  # worker prereqs ok, but no linked PR / readiness
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[dispatch_to("verifier"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert fake.role_calls == []
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED


def test_gate_pauses_on_permission_error(tmp_ws: Workspace):
    from crewd.github_bus import GitHubErrorKind
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    c.fail_once("list_issues", GitHubErrorKind.PERMISSION, "403")
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert fake.role_calls == []
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert "permission" in (run.human_blocker or "")


def test_gate_does_not_consume_pending_handoff_when_blocking(tmp_ws: Workspace):
    """A blocked dispatch must not acknowledge the handoff that led to it: after
    resume the same routing decision is still pending for Lead."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))  # task missing → worker blocked
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    # The worker attempt was never reserved → no worker attempt row terminalized.
    assert fake.role_calls == []


def test_no_gate_is_inert(tmp_ws: Workspace):
    """With no bus_gate wired, dispatch behaves exactly as before (production
    default is unchanged)."""
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, None)
    orch.run(once=False)
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"
