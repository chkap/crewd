"""Bounded, no-false-human-PAUSE Lead recovery (issue #65).

Builds on the typed correction contract from #64. These deterministic fake-SDK
tests prove that *internal* recovery classes — repeated missing/malformed Lead
decisions, transport (SDK) aborts, and thrash/no-progress guards — settle into a
bounded ``WAITING`` state with an observable wake condition rather than a
fabricated human ``PAUSE``; that an explicit resume restores the per-class retry
budgets without erasing durable evidence; that a genuine operator-only
prerequisite (an explicit Lead ``pause``) still PAUSES; and that model-selected
``continue_lead`` was removed from the Lead decision contract in favour of
host-managed no-decision re-solicitation.

Includes a subset of the live #64 incident chain: two clean Worker SDK aborts
followed by an attempt that durably produces branch/PR evidence but double-calls
``submit_role_handoff`` — the double submission is a protocol failure (uncertain)
that must NOT fabricate a human pause and must preserve the durable evidence in
the pending handoff so Lead can route on it instead of blindly repeating Worker.
"""
from __future__ import annotations

import pytest

from crewd.config import CrewConfig, GoalState
from crewd.dispatcher import (
    Dispatcher,
    DispatcherLimits,
    LEAD_PENDING,
    LeadDecision,
    RunStatus,
)
from crewd.executor import parse_lead_decision
from crewd.orchestrator import Orchestrator
from crewd.session_backend import AttemptOutcome
from crewd.workspace import Workspace

from fakes import FakeExecutor, dispatch_to, finish, pause, role_handoff

ROLES = ("lead", "advisory", "worker", "verifier")


def _orch(tmp_ws: Workspace, fake: FakeExecutor, *, limits=None, max_steps=200,
          label="goal:v1") -> tuple[Orchestrator, Dispatcher]:
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = 0
    gs = GoalState(version=1, label=label, cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db", limits=limits)
    orch = Orchestrator(tmp_ws, cfg, fake, gs, dispatcher=disp, max_steps=max_steps)
    return orch, disp


# ── no false human PAUSE: internal recovery classes settle into bounded WAIT ──
def test_repeated_missing_decision_waits_never_pauses(tmp_ws: Workspace):
    """Repeated clean turns that omit a structured decision are host-managed
    recovery, not an operator prerequisite: the run settles into WAITING with an
    observable wake condition, never a human PAUSE."""
    fake = FakeExecutor(lead_script=[None] * 10)
    orch, disp = _orch(tmp_ws, fake, limits=DispatcherLimits(max_invalid_solicitations=3))
    assert orch.run(once=False) == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"
    run = disp.start_or_resume_run("goal:v1")
    assert RunStatus(run.status) is RunStatus.WAITING
    assert run.human_blocker is None
    assert run.wake_condition
    assert run.invalid_solicitations >= 3


def test_repeated_sdk_abort_waits_never_pauses(tmp_ws: Workspace):
    """Worker attempts that repeatedly abort at the SDK transport are unproductive;
    the no-progress/thrash guard bounds them into WAITING, not a human PAUSE."""
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker")] * 30,
        role_outcome=AttemptOutcome.ABORTED_CLEAN,
    )
    orch, disp = _orch(tmp_ws, fake, max_steps=100)
    assert orch.run(once=False) == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"
    run = disp.start_or_resume_run("goal:v1")
    assert RunStatus(run.status) is RunStatus.WAITING
    assert run.human_blocker is None


def test_explicit_lead_pause_still_pauses(tmp_ws: Workspace):
    """A genuine operator-only prerequisite — an explicit Lead ``pause`` — is the
    one path that still records a human blocker and PAUSES."""
    fake = FakeExecutor(lead_script=[pause("needs a human credential decision")])
    orch, disp = _orch(tmp_ws, fake)
    assert orch.run(once=False) == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"
    run = disp.start_or_resume_run("goal:v1")
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert run.human_blocker == "needs a human credential decision"


# ── explicit resume restores per-class budgets without erasing evidence ──
def test_resume_restores_invalid_solicitation_budget(tmp_ws: Workspace):
    """After the invalid-solicitation cap settles the run into WAITING, an explicit
    resume restores a fresh per-class budget (invalid_solicitations == 0) and
    reactivates the run so a later clean decision can make progress."""
    fake = FakeExecutor(lead_script=[None] * 10)
    orch, disp = _orch(tmp_ws, fake, limits=DispatcherLimits(max_invalid_solicitations=2))
    assert orch.run(once=False) == 0
    run = disp.start_or_resume_run("goal:v1")
    assert RunStatus(run.status) is RunStatus.WAITING
    assert run.invalid_solicitations >= 2

    resumed = disp.resume_run(run.id)
    assert RunStatus(resumed.status) is RunStatus.ACTIVE
    assert resumed.invalid_solicitations == 0
    assert resumed.routing_authority == LEAD_PENDING


def test_resume_preserves_durable_handoff_evidence(tmp_ws: Workspace):
    """Resume refreshes live budgets but must NOT erase historical evidence: the
    synthetic thrash-guard handoffs remain in the durable journal after resume."""
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker")] * 30,
        role_outcome=AttemptOutcome.SDK_ERROR,
    )
    orch, disp = _orch(tmp_ws, fake, max_steps=100)
    orch.run(once=False)
    run = disp.start_or_resume_run("goal:v1")
    before = len(disp.export_run(run.id)["handoffs"])
    assert before > 0
    resumed = disp.resume_run(run.id)
    assert RunStatus(resumed.status) is RunStatus.ACTIVE
    after = len(disp.export_run(run.id)["handoffs"])
    assert after == before  # evidence preserved, only live budgets refreshed


# ── removal of model-selected continue_lead ──
def test_continue_lead_is_not_model_selectable():
    """The Lead decision contract no longer accepts a model-selected continue_lead;
    parsing raises so the executor treats it as a no-decision (host re-solicits)."""
    with pytest.raises(ValueError):
        parse_lead_decision({"kind": "continue_lead", "ack_handoff_ids": []})


def test_continue_lead_kernel_kind_retained_for_compat(tmp_path):
    """The kernel ``continue_lead`` mechanism is retained for historical journal
    compatibility: applied via the kernel API (bypassing the model-facing parse)
    it still creates a self-dispatch to Lead and keeps the run ACTIVE, so an old
    journal carrying a continue_lead row still loads and reconciles."""
    disp = Dispatcher(tmp_path / "dispatch.db")
    run = disp.start_or_resume_run("goal:v1")
    res = disp.lead_decide(run.id, LeadDecision.continue_lead(), configured_roles=ROLES)
    assert res.dispatch is not None
    assert res.dispatch.role == "lead"
    got = disp.get_run(run.id)
    assert RunStatus(got.status) is RunStatus.ACTIVE
    disp.close()


# ── #64 incident-chain subset: two clean SDK aborts, then a double role handoff
#    carrying durable branch/PR evidence must stay uncertain, preserve the
#    evidence, and never fabricate a human pause. ──
def test_double_role_handoff_is_uncertain_preserves_evidence_no_false_pause(tmp_ws: Workspace):
    evidence = "branch crewd/x; PR #99 mergeable, checks green; task #65 bound"
    # Two clean Worker SDK aborts, then every later Worker tick durably produces
    # branch/PR evidence but double-calls submit_role_handoff (submissions=2).
    outcomes = iter([AttemptOutcome.ABORTED_CLEAN, AttemptOutcome.ABORTED_CLEAN])

    def role_outcome(req):
        return next(outcomes, AttemptOutcome.IDLE_COMPLETED)

    fake = FakeExecutor(
        lead_script=[dispatch_to("worker")] * 30,
        role_outcome=role_outcome,
        role_handoff=role_handoff("completed", evidence=evidence, changed="opened PR #99"),
        role_handoff_submissions=2,  # double submission → protocol failure
    )
    orch, disp = _orch(tmp_ws, fake, max_steps=100)
    assert orch.run(once=False) == 0

    # No fabricated human pause: the run settled into a bounded WAIT.
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"
    run = disp.start_or_resume_run("goal:v1")
    assert RunStatus(run.status) is RunStatus.WAITING
    assert run.human_blocker is None

    # Every double-submission Worker handoff is uncertain (a double submission is
    # never a silent completion); the two leading SDK aborts are no_progress. No
    # Worker attempt is ever a fabricated completion, and the durable branch/PR
    # evidence is preserved so Lead can route on it (independent Verifier review)
    # instead of blindly repeating Worker.
    worker_handoffs = [
        h for h in disp.export_run(run.id)["handoffs"] if h["role"] == "worker"
    ]
    assert worker_handoffs
    assert all(h["outcome_class"] != "completed" for h in worker_handoffs)
    assert any(h["outcome_class"] == "uncertain" for h in worker_handoffs)
    assert any(evidence in (h["evidence"] or "") for h in worker_handoffs)


def test_double_role_handoff_recovers_on_resume(tmp_ws: Workspace):
    """After the double-submission thrash settles into WAITING, an explicit resume
    restores the budget and a subsequent clean single-submission completion is
    accepted — recovery is possible without operator intervention."""
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker")] * 30,
        role_handoff=role_handoff("completed", evidence="PR #99", changed="opened PR"),
        role_handoff_submissions=2,
    )
    orch, disp = _orch(tmp_ws, fake, max_steps=100)
    orch.run(once=False)
    run = disp.start_or_resume_run("goal:v1")
    assert RunStatus(run.status) is RunStatus.WAITING
    resumed = disp.resume_run(run.id)
    assert RunStatus(resumed.status) is RunStatus.ACTIVE
    assert resumed.consecutive_unproductive == 0
    assert resumed.last_edge_repeats == 0
