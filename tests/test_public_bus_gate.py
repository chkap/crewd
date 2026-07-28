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

from fakes import FakeExecutor, dispatch_to, finish, wait
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

    # Worker dispatch is allowed by a valid record; the run then waits (finish is
    # exercised separately below, since a valid finish requires a *closed*
    # umbrella which cannot coexist with the open umbrella a worker needs).
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), wait("external")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert [r.role for r in fake.role_calls] == ["worker"]
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"


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


# ── attributed public-record helpers ──
def _assign(c: FakeGitHubClient, task: int) -> None:
    """A public Lead worker-assignment record on the task issue."""
    c.add_comment("issue", task, f"> **[crewd:lead -> worker]** {CREW}\n\nAssigned.")


def _readiness(c: FakeGitHubClient, task: int) -> None:
    c.add_comment("issue", task, f"> **[crewd:worker -> verifier]** {CREW}\n\nReady.")


def _summary(c: FakeGitHubClient, acc: int) -> None:
    c.add_comment("issue", acc, f"> **[crewd:lead -> all]** {CREW}\n\nGoal complete.")


def _finish_ready_record(c: FakeGitHubClient) -> None:
    """Closed umbrella + public goal summary → finish prerequisite satisfied."""
    c.add_issue(30, "GOAL: x", state="closed", labels=(GOAL,))
    _summary(c, 30)


def test_finish_blocked_without_final_acceptance(tmp_ws: Workspace):
    """A Lead finish must not terminalise the run when the umbrella is still open
    / has no public summary; the run pauses and is NOT goal-complete."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))  # umbrella open, no summary
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert "public-bus" in (run.human_blocker or "")
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


def test_finish_allowed_when_acceptance_closed_with_summary(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    _finish_ready_record(c)
    gate = PublicBusGate(_bus(c))  # references resolved from the record

    fake = FakeExecutor(lead_script=[finish("accepted")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"


def test_finish_block_does_not_consume_handoff_or_finish(tmp_ws: Workspace):
    """After a blocked finish, authority is still Lead's and the run is not
    finished — the finish decision was refused, not applied."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))  # open, no summary
    gate = PublicBusGate(_bus(c))

    fake = FakeExecutor(lead_script=[finish("done"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is not RunStatus.FINISHED
    assert RunStatus(run.status) is RunStatus.PAUSED


# ── typed reference resolution (no hard-coded task number) ──
def test_resolver_gate_allows_worker_when_single_active_task(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))
    _assign(c, TASK)  # public Lead assignment makes #29 the active task
    gate = PublicBusGate(_bus(c))  # no task_number → derived from record

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), wait("external")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert [r.role for r in fake.role_calls] == ["worker"]


def test_resolver_gate_blocks_worker_when_no_active_task(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))  # no Lead assignment
    gate = PublicBusGate(_bus(c))

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert fake.role_calls == []
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED


def test_resolver_gate_blocks_worker_when_multiple_active_tasks(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(29, "task a", labels=("crewd:task", GOAL))
    c.add_issue(28, "task b", labels=("crewd:task", GOAL))
    _assign(c, 29)
    _assign(c, 28)  # two actively-assigned tasks → ambiguous → block
    gate = PublicBusGate(_bus(c))

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert fake.role_calls == []
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert "multiple" in (run.human_blocker or "").lower()


def test_resolver_gate_allows_verifier_with_pr_and_readiness(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))
    _assign(c, TASK)
    c.add_pull(101, "impl", linked_issues=(TASK,))
    _readiness(c, TASK)
    gate = PublicBusGate(_bus(c))

    fake = FakeExecutor(lead_script=[dispatch_to("verifier"), wait("external")])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert [r.role for r in fake.role_calls] == ["verifier"]
