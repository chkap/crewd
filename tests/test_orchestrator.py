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
    role_handoff,
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


# ── stop sentinel halts before work and records STOPPED (distinct reason) ──
def test_stop_sentinel_exits_stopped(tmp_ws: Workspace):
    tmp_ws.stop("operator")
    fake = FakeExecutor(lead_script=[dispatch_to("worker")])
    orch = _orch(tmp_ws, fake)
    orch.disp.start_or_resume_run("goal:v1")
    tmp_ws.stop("operator")
    rc = orch.run(once=False)
    assert rc == 0
    # Operator stop is distinct from goal completion — never "goal-complete".
    assert tmp_ws.exit_reason_file.read_text().strip() == "stopped"
    assert fake.lead_calls == []  # halted before any Lead work
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

    def wrapped(req, *, on_started=None, cancel=None):
        state["n"] += 1
        if state["n"] >= 2:
            orch._interrupted = True
        return orig(req, on_started=on_started, cancel=cancel)

    fake.run_lead = wrapped  # type: ignore[method-assign]
    rc = orch.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "interrupted"
    assert _final_run(tmp_ws).status == RunStatus.INTERRUPTED.value


# ── pre-send journaling: mark_started persists BEFORE the SDK body runs ──
def test_attempt_journaled_started_before_send(tmp_ws: Workspace):
    from crewd.dispatcher import AttemptState
    from crewd.executor import AttemptRequest

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish()])
    orch = _orch(tmp_ws, fake)
    observed: dict = {}

    orig = fake.execute_role

    def spy(req: AttemptRequest, *, on_started=None, cancel=None):
        order: list[str] = observed.setdefault("order", [])

        def _wrapped_on_started(session_id, generation):
            order.append("journaled")
            observed["session_id"] = session_id
            on_started(session_id, generation)

        order.append("body-start")  # executor body begins after on_started
        return orig(req, on_started=_wrapped_on_started, cancel=cancel)

    fake.execute_role = spy  # type: ignore[method-assign]
    rc = orch.run(once=False)
    assert rc == 0
    # on_started (journaling) runs during execute_role, before the outcome is
    # recorded terminal; the worker attempt therefore passed through STARTED.
    assert observed.get("session_id") == "sess-worker"
    assert "journaled" in observed.get("order", [])


def test_on_started_failure_surfaces_not_swallowed(tmp_ws: Workspace):
    # If durable journaling fails, the error must propagate (not be swallowed) so
    # the run never continues with an unjournaled in-flight attempt.
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish()])
    orch = _orch(tmp_ws, fake)

    orig = fake.execute_role

    def boom(req, *, on_started=None, cancel=None):
        def _failing(session_id, generation):
            raise RuntimeError("journal write failed")

        return orig(req, on_started=_failing, cancel=cancel)

    fake.execute_role = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="journal write failed"):
        orch.run(once=False)


# ── no auto-resume: a plain run must not revive a paused/stopped run ──
def test_plain_run_does_not_resume_paused(tmp_ws: Workspace):
    # First run: Lead pauses → durable PAUSED.
    fake1 = FakeExecutor(lead_script=[pause("human-blocked: need input")])
    orch1 = _orch(tmp_ws, fake1)
    assert orch1.run(once=False) == 0
    assert _final_run(tmp_ws).status == RunStatus.PAUSED.value

    # Second plain run (resume=False default): must NOT erase the blocker.
    fake2 = FakeExecutor(lead_script=[finish("sneaky")])
    orch2 = _orch(tmp_ws, fake2)
    rc = orch2.run(once=False)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"
    assert fake2.lead_calls == []  # no work; blocker preserved
    assert _final_run(tmp_ws).status == RunStatus.PAUSED.value

    # Explicit resume=True revives it and work proceeds to finish.
    fake3 = FakeExecutor(lead_script=[finish("accepted")])
    orch3 = _orch(tmp_ws, fake3)
    rc = orch3.run(once=False, resume=True)
    assert rc == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "goal-complete"
    assert _final_run(tmp_ws).status == RunStatus.FINISHED.value

# ── in-flight cancellation: interrupt cancels the active attempt (worker thread) ──
def test_interrupt_cancels_inflight_attempt_cleanly(tmp_ws: Workspace):
    # A role attempt blocks until cancelled; the orchestrator's control poll must
    # trip the CancelToken (interrupt) so the attempt returns CANCELLED_CLEAN,
    # which is recorded as a distinct `cancelled` handoff (never `completed`).
    fake = FakeExecutor(lead_script=[dispatch_to("worker")], block_until_cancel=True)
    orch = _orch(tmp_ws, fake)

    # Trip the interrupt shortly after the worker attempt begins (on another
    # thread) so the poll loop requests cancellation while it is in flight.
    import threading

    def _interrupt_soon():
        while not fake.role_calls:
            pass
        orch._interrupted = True

    threading.Thread(target=_interrupt_soon, daemon=True).start()
    rc = orch.run(once=False)
    assert rc == 0
    # The worker ran exactly once (blocked, then cancelled — never replayed).
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert tmp_ws.exit_reason_file.read_text().strip() == "interrupted"
    # The cancelled attempt produced a distinct `cancelled` handoff class.
    from crewd.dispatcher import HandoffOutcome

    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    try:
        run = disp.start_or_resume_run("goal:v1")
        rows = disp._conn.execute(
            "SELECT outcome_class FROM handoff WHERE run_id = ?", (run.id,)
        ).fetchall()
        classes = {r["outcome_class"] for r in rows}
    finally:
        disp.close()
    assert HandoffOutcome.CANCELLED.value in classes
    assert HandoffOutcome.COMPLETED.value not in classes


def test_operator_stop_cancels_inflight_attempt(tmp_ws: Workspace):
    # An operator STOP sentinel raised mid-attempt must also cancel the in-flight
    # attempt (same single owner) and exit `stopped`.
    fake = FakeExecutor(lead_script=[dispatch_to("worker")], block_until_cancel=True)
    orch = _orch(tmp_ws, fake)

    import threading

    def _stop_soon():
        while not fake.role_calls:
            pass
        tmp_ws.stop("operator requested")

    threading.Thread(target=_stop_soon, daemon=True).start()
    rc = orch.run(once=False)
    assert rc == 0
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert tmp_ws.exit_reason_file.read_text().strip() == "stopped"


# ── restart recovery: orphaned `started` session is tainted before finalize ──
def test_restart_taints_orphan_session_before_finalize(tmp_ws: Workspace):
    # Drive the kernel to an in-flight (started) worker attempt with a known
    # session id, then run a fresh orchestrator: the orphan session must be
    # tainted BEFORE the uncertain handoff is finalized, and recovery must never
    # resume the orphan generation normally.
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
    disp.mark_started(attempt_id, session_id="orphan-sess-worker-g0", generation=0)
    disp.close()

    # Record which orphans were tainted, in order relative to finalize. We assert
    # the taint callback fires for the started orphan with its session identity.
    tainted: list[tuple[str, int, str]] = []

    fake = FakeExecutor(lead_script=[pause("human-blocked: after crash")])
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    gs = GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    orch = Orchestrator(
        tmp_ws, cfg, fake, gs,
        taint_orphan=lambda sid, gen, role: tainted.append((sid, gen, role)),
    )
    rc = orch.run(once=False)
    assert rc == 0
    assert fake.role_calls == []  # no replay
    # The orphaned started session was tainted with its exact identity.
    assert ("orphan-sess-worker-g0", 0, "worker") in tainted
    disp2 = Dispatcher(db)
    att = disp2.get_attempt(attempt_id)
    assert att.state == AttemptState.RECONCILED_UNCERTAIN
    disp2.close()


def test_restart_recovery_is_idempotent(tmp_ws: Workspace):
    # Recovery itself may crash and be retried: reconcile_on_restart must be
    # idempotent (re-taint is a no-op, no duplicate uncertain handoff on the
    # already-reconciled attempt).
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
    disp.mark_started(attempt_id, session_id="orphan-2", generation=0)

    calls: list[str] = []
    taint = lambda sid, gen, role: calls.append(sid)
    first = disp.reconcile_on_restart(run.id, taint_orphan=taint)
    second = disp.reconcile_on_restart(run.id, taint_orphan=taint)
    assert len(first) == 1  # one uncertain handoff created
    assert second == []     # nothing left in-flight; idempotent
    # The orphan was only in `started` on the first pass, so tainted once.
    assert calls == ["orphan-2"]
    disp.close()


# ── #12: structured role handoff survives into SQLite + next Lead prompt ──
def _handoff_rows(tmp_ws: Workspace) -> list[dict]:
    """Read the durable handoff rows straight from the run DB (out of band)."""
    import sqlite3

    con = sqlite3.connect(tmp_ws.state_dir / "dispatch.db")
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT role, outcome_class, evidence, changed, remaining, reason_returned "
            "FROM handoff ORDER BY created_at"
        )]
    finally:
        con.close()


def test_role_handoff_payload_survives_to_sqlite_and_next_lead_prompt(tmp_ws: Workspace):
    # The strongest oracle for #12: a role's structured tool payload must be
    # journaled verbatim in SQLite AND rendered verbatim in Lead's next
    # solicitation prompt — rendering assertions alone are too weak.
    hp = role_handoff(
        "completed",
        evidence="PR #42 green; 12 tests",
        changed="added dispatcher kernel",
        remaining="wire docs",
        reason="ready for verifier",
    )
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), finish("accepted")], role_handoff=hp
    )
    orch = _orch(tmp_ws, fake)
    assert orch.run(once=False) == 0

    # 1) Durable in SQLite as a productive completion with the role's fields.
    rows = _handoff_rows(tmp_ws)
    worker_rows = [r for r in rows if r["role"] == "worker"]
    assert len(worker_rows) == 1
    wr = worker_rows[0]
    assert wr["outcome_class"] == "completed"
    assert wr["evidence"] == "PR #42 green; 12 tests"
    assert wr["changed"] == "added dispatcher kernel"
    assert wr["remaining"] == "wire docs"
    assert wr["reason_returned"] == "ready for verifier"

    # 2) Verbatim in Lead's next solicitation prompt (the finish turn).
    assert len(fake.lead_calls) == 2
    prompt = fake.lead_calls[1].prompt
    assert "PR #42 green; 12 tests" in prompt
    assert "added dispatcher kernel" in prompt
    assert "wire docs" in prompt
    assert "ready for verifier" in prompt


def test_role_no_handoff_on_clean_idle_records_uncertain(tmp_ws: Workspace):
    # A role that returns a clean idle without a structured handoff must NOT be
    # recorded as a completion — it is an `uncertain` protocol failure.
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), finish("accepted")], role_handoff=None
    )
    orch = _orch(tmp_ws, fake)
    assert orch.run(once=False) == 0
    worker_rows = [r for r in _handoff_rows(tmp_ws) if r["role"] == "worker"]
    assert len(worker_rows) == 1
    assert worker_rows[0]["outcome_class"] == "uncertain"
    assert "role_protocol_failure" in worker_rows[0]["reason_returned"]


def test_transport_error_overrides_role_success_claim_end_to_end(tmp_ws: Workspace):
    # Role shapes a "completed" handoff but the transport actually errored — the
    # durable handoff must be `failed`, not `completed`.
    hp = role_handoff("completed", evidence="I did it")
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), finish("accepted")],
        role_outcome=AttemptOutcome.SDK_ERROR,
        role_handoff=hp,
    )
    orch = _orch(tmp_ws, fake)
    assert orch.run(once=False) == 0
    worker_rows = [r for r in _handoff_rows(tmp_ws) if r["role"] == "worker"]
    assert worker_rows[0]["outcome_class"] == "failed"
    assert worker_rows[0]["reason_returned"] == "sdk:sdk_error"
