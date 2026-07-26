"""End-to-end fake-SDK matrix for the dispatcher-driven orchestrator.

Exercises the whole routing/restart surface with no SDK: Lead solicitation →
decision → role dispatch → terminal handoff → next Lead decision, plus the
terminal conditions (finish/wait/pause/stop/exhausted/interrupted), invalid
solicitations, guard trips, and restart reconciliation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from crewd.config import CrewConfig, GoalState
from crewd.dispatcher import AttemptState, Dispatcher, DispatcherLimits, RunStatus
from crewd.orchestrator import Orchestrator
from crewd.session_backend import AttemptOutcome
from crewd.workspace import Workspace

from fakes import (
    FakeExecutor,
    continue_lead,
    dispatch_to,
    finish,
    pause,
    wait,
)


def _orch(tmp_ws: Workspace, fake: FakeExecutor, *, max_cycles: int = 0,
          label: str = "goal:v1", max_steps: int = 200) -> Orchestrator:
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = max_cycles
    gs = GoalState(version=1, label=label, cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    return Orchestrator(tmp_ws, cfg, fake, gs, max_steps=max_steps)


def _final_run(tmp_ws: Workspace, label: str = "goal:v1"):
    """Reopen a fresh Dispatcher on the run db to inspect final durable state
    (the orchestrator closes its own dispatcher on exit)."""
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    try:
        return disp.start_or_resume_run(label)
    finally:
        disp.close()


# ── happy path: dispatch → complete → finish ──
def test_dispatch_complete_then_finish(tmp_ws: Workspace):
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("accepted")])
    orch = _orch(tmp_ws, fake)
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"
    # Worker ran exactly once; Lead solicited twice (dispatch, then finish).
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert len(fake.lead_calls) == 2
    run = _final_run(tmp_ws)
    assert run.status == RunStatus.FINISHED.value


# ── lead pause → human-blocked ──
def test_lead_pause_exits_human_blocked(tmp_ws: Workspace):
    fake = FakeExecutor(lead_script=[pause("human-blocked: approval needed")])
    orch = _orch(tmp_ws, fake)
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"
    assert fake.role_calls == []
    assert _final_run(tmp_ws).status == RunStatus.PAUSED.value


# ── lead wait → waiting ──
def test_lead_wait_exits_waiting(tmp_ws: Workspace):
    fake = FakeExecutor(lead_script=[wait("pr-review")])
    orch = _orch(tmp_ws, fake)
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"
    assert _final_run(tmp_ws).status == RunStatus.WAITING.value


# ── budget exhaustion ──
def test_max_work_exhaustion(tmp_ws: Workspace):
    # Lead keeps dispatching worker; a tiny work budget must exhaust the run.
    fake = FakeExecutor(lead_script=[dispatch_to("worker")] * 20)
    orch = _orch(tmp_ws, fake, max_cycles=2)
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "exhausted"
    assert _final_run(tmp_ws).status == RunStatus.EXHAUSTED.value


# ── role failure produces an unproductive handoff Lead can act on ──
def test_role_failure_then_lead_pauses(tmp_ws: Workspace):
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), pause("human-blocked: worker failing")],
        role_outcome=AttemptOutcome.SDK_ERROR,
    )
    orch = _orch(tmp_ws, fake)
    rc = orch.run(once=False)
    assert rc == 0
    # The failed attempt still terminalised (no replay) and Lead got the handoff.
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


# ── invalid solicitations (no decision) pause after the cap ──
def test_invalid_solicitations_pause_after_cap(tmp_ws: Workspace):
    # Every Lead turn returns None (no decision) → invalid; cap pauses the run.
    fake = FakeExecutor(lead_script=[None] * 10)
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    gs = GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    disp = Dispatcher(
        tmp_ws.state_dir / "dispatch.db",
        limits=DispatcherLimits(max_invalid_solicitations=3),
    )
    orch = Orchestrator(tmp_ws, cfg, fake, gs, dispatcher=disp, max_steps=50)
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"
    run = disp.get_run(disp.start_or_resume_run("goal:v1").id)
    assert run.status == RunStatus.PAUSED.value
    assert run.invalid_solicitations >= 3


# ── once=True advances exactly one step ──
def test_once_advances_single_step(tmp_ws: Workspace):
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish()])
    orch = _orch(tmp_ws, fake)
    rc = orch.run(once=True)
    assert rc == 0
    # One step = the Lead solicitation that dispatches worker; worker has NOT run.
    assert len(fake.lead_calls) == 1
    assert fake.role_calls == []
    assert _final_run(tmp_ws).status == RunStatus.ACTIVE.value


# ── stop sentinel halts before work and records STOPPED ──
def test_stop_sentinel_exits_goal_complete(tmp_ws: Workspace):
    tmp_ws.stop("operator")
    fake = FakeExecutor(lead_script=[dispatch_to("worker")])
    orch = _orch(tmp_ws, fake)
    # `crewd run` clears sentinels itself; here we drive the orchestrator
    # directly, so re-assert the sentinel after resume to simulate a stop that
    # arrives during the run.
    orch.disp.start_or_resume_run("goal:v1")
    tmp_ws.stop("operator")
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"
    assert _final_run(tmp_ws).status == RunStatus.STOPPED.value


# ── dispatch to a configured non-worker role (advisory) works ──
def test_dispatch_to_advisory(tmp_ws: Workspace):
    fake = FakeExecutor(lead_script=[dispatch_to("advisory"), finish()])
    orch = _orch(tmp_ws, fake)
    rc = orch.run(once=False)
    assert rc == 0
    assert [r.role for r in fake.role_calls] == ["advisory"]


# ── continue_lead returns authority to Lead (another budgeted turn) ──
def test_continue_lead_then_finish(tmp_ws: Workspace):
    fake = FakeExecutor(lead_script=[continue_lead(), finish()])
    orch = _orch(tmp_ws, fake)
    rc = orch.run(once=False)
    assert rc == 0
    # Two Lead turns, no role work.
    assert len(fake.lead_calls) == 2
    assert fake.role_calls == []
    assert _final_run(tmp_ws).status == RunStatus.FINISHED.value


# ── restart reconciliation: an in-flight attempt is not replayed ──
def test_restart_reconciles_inflight_attempt(tmp_ws: Workspace):
    # Manually drive the kernel to an in-flight (started, no terminal) worker
    # attempt, then run a fresh orchestrator over the SAME db.
    db = tmp_ws.state_dir / "dispatch.db"
    tmp_ws.state_dir.mkdir(parents=True, exist_ok=True)
    disp = Dispatcher(db)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    from crewd.dispatcher import LeadDecision

    disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.dispatch("worker", ack=()),
        configured_roles=("lead", "worker", "verifier", "advisory"),
    )
    run = disp.get_run(run.id)
    attempt_id = disp.reserve_attempt(run.id, run.routing_authority, "worker")
    disp.mark_started(attempt_id, session_id="sess-worker", generation=0)
    disp.close()

    # Fresh orchestrator: reconcile must convert the orphan to uncertain, and
    # Lead then pauses. The worker attempt is NEVER re-executed.
    fake = FakeExecutor(lead_script=[pause("human-blocked: after crash")])
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    gs = GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    orch = Orchestrator(tmp_ws, cfg, fake, gs)
    rc = orch.run(once=False)
    assert rc == 0
    assert fake.role_calls == []  # no replay
    disp2 = Dispatcher(db)
    att = disp2.get_attempt(attempt_id)
    assert att.state == AttemptState.RECONCILED_UNCERTAIN
    disp2.close()


# ── thrash guard: repeated identical edge trips a synthetic pause ──
def test_thrash_guard_pauses(tmp_ws: Workspace):
    # Lead dispatches worker every time; worker always reports no progress
    # (SDK_ERROR → unproductive). The edge-repeat / unproductive guard must
    # eventually pause the run rather than dispatch forever.
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker")] * 30,
        role_outcome=AttemptOutcome.SDK_ERROR,
    )
    orch = _orch(tmp_ws, fake, max_steps=100)
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"
    assert _final_run(tmp_ws).status == RunStatus.PAUSED.value


# ── signal interrupt mid-loop exits interrupted ──
def test_signal_interrupt_exits_interrupted(tmp_ws: Workspace):
    fake = FakeExecutor(lead_script=[dispatch_to("worker")] * 50)
    orch = _orch(tmp_ws, fake, max_steps=1000)

    # Trip the interrupt flag after the first Lead turn by wrapping run_lead.
    orig = fake.run_lead
    state = {"n": 0}

    def wrapped(req):
        state["n"] += 1
        if state["n"] >= 2:
            orch._interrupted = True
        return orig(req)

    fake.run_lead = wrapped  # type: ignore[method-assign]
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "interrupted"
    assert _final_run(tmp_ws).status == RunStatus.INTERRUPTED.value
