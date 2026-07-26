"""Crash-point and invariant tests for the durable dispatch kernel (#11 slice A).

A *crash* is modelled by discarding the :class:`Dispatcher` (and its SQLite
connection) mid-workflow and reopening a fresh instance on the same database
file. Because every mutating method commits as one transaction, whatever was
committed must survive and reconcile deterministically; whatever was mid-flight
must roll back cleanly.
"""
from __future__ import annotations

import pytest

from crewd.session_backend import AttemptOutcome
from crewd.dispatcher import (
    Dispatcher,
    DispatcherLimits,
    DecisionKind,
    LeadDecision,
    HandoffOutcome,
    AttemptState,
    RunStatus,
    BudgetExhausted,
    DecisionError,
    classify,
    LEAD_PENDING,
)

ROLES = ("lead", "advisory", "worker", "verifier")


def _open(tmp_path, **limits):
    return Dispatcher(tmp_path / "dispatch.sqlite3", DispatcherLimits(**limits) if limits else None)


def _drive(disp, run_id, dispatch_id, role, outcome, *, stop_after=None, **ho):
    """Reserve → start → terminal, optionally stopping early to model a crash."""
    att = disp.reserve_attempt(run_id, dispatch_id, role)
    if stop_after == "reserve":
        return att
    disp.mark_started(att, session_id="sess-x", generation=0)
    if stop_after == "start":
        return att
    disp.record_terminal(att, outcome, **ho)
    return att


# ─────────────────────────── run lifecycle ───────────────────────────
def test_start_run_creates_then_resumes_same(tmp_path):
    disp = _open(tmp_path)
    r1 = disp.start_or_resume_run("goal:v1")
    r2 = disp.start_or_resume_run("goal:v1")
    assert r1.id == r2.id
    assert r1.status is RunStatus.ACTIVE
    assert r1.routing_authority == LEAD_PENDING


def test_new_goal_epoch_creates_fresh_run(tmp_path):
    disp = _open(tmp_path)
    r1 = disp.start_or_resume_run("goal:v1")
    r2 = disp.start_or_resume_run("goal:v2")
    assert r1.id != r2.id


def test_state_survives_reopen(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    disp.close()

    disp2 = _open(tmp_path)
    got = disp2.get_run(run.id)
    assert got.routing_authority == d.dispatch.id
    assert got.status is RunStatus.ACTIVE


# ─────────────────────────── happy path / routing ───────────────────────────
def test_full_handoff_cycle(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")

    d1 = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    assert disp.get_run(run.id).routing_authority == d1.dispatch.id

    att = _drive(disp, run.id, d1.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED,
                 evidence="PR #99", changed="src/x.py", remaining="review")
    # terminal returns authority to Lead
    assert disp.get_run(run.id).routing_authority == LEAD_PENDING
    assert disp.get_attempt(att).state is AttemptState.TERMINAL

    pending = disp.pending_handoffs(run.id)
    assert len(pending) == 1
    assert pending[0].outcome_class is HandoffOutcome.COMPLETED

    d2 = disp.lead_decide(
        run.id, LeadDecision.dispatch("verifier", ack=(pending[0].id,)), configured_roles=ROLES
    )
    assert d2.dispatch.role == "verifier"
    assert disp.pending_handoffs(run.id) == []  # acked → consumed


def test_completed_resets_unproductive_counter(tmp_path):
    disp = _open(tmp_path, max_consecutive_unproductive=5)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.SDK_ERROR)
    assert disp.get_run(run.id).consecutive_unproductive == 1
    d2 = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    _drive(disp, run.id, d2.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED)
    assert disp.get_run(run.id).consecutive_unproductive == 0


# ─────────────────────────── invariants ───────────────────────────
def test_exactly_one_terminal(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    att = _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED)
    with pytest.raises(DecisionError):
        disp.record_terminal(att, AttemptOutcome.SDK_ERROR)


def test_invalid_dispatch_role_keeps_authority_with_lead(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    with pytest.raises(DecisionError):
        disp.lead_decide(run.id, LeadDecision.dispatch("nonrole"), configured_roles=ROLES)
    got = disp.get_run(run.id)
    assert got.routing_authority == LEAD_PENDING
    assert disp.export_run(run.id)["attempts"] == []


def test_classify_mapping():
    assert classify(AttemptOutcome.IDLE_COMPLETED) is HandoffOutcome.COMPLETED
    assert classify(AttemptOutcome.SDK_ERROR) is HandoffOutcome.FAILED
    assert classify(AttemptOutcome.ABORTED_CLEAN) is HandoffOutcome.TIMED_OUT
    assert classify(AttemptOutcome.TAINTED) is HandoffOutcome.UNCERTAIN


# ─────────────────────────── durable budget ───────────────────────────
def test_max_work_reservation_is_not_refunded_by_crash(tmp_path):
    disp = _open(tmp_path, max_work=2)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    # First slot: crash right after reservation (before start).
    _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED, stop_after="reserve")
    disp.close()

    # Reopen: reserved slot persists, budget not refunded.
    disp2 = _open(tmp_path, max_work=2)
    assert disp2.get_run(run.id).reserved_slots == 1
    disp2.reserve_attempt(run.id, d.dispatch.id, "worker")  # 2nd slot ok
    with pytest.raises(BudgetExhausted):
        disp2.reserve_attempt(run.id, d.dispatch.id, "worker")  # 3rd over budget
    assert disp2.get_run(run.id).status is RunStatus.EXHAUSTED


# ─────────────────────────── crash-point reconciliation ───────────────────────────
@pytest.mark.parametrize("stop_after", ["reserve", "start"])
def test_reconcile_marks_inflight_uncertain(tmp_path, stop_after):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    att = _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED,
                 stop_after=stop_after)
    disp.close()

    disp2 = _open(tmp_path)
    created = disp2.reconcile_on_restart(run.id)
    assert len(created) == 1
    assert disp2.get_attempt(att).state is AttemptState.RECONCILED_UNCERTAIN
    ho = disp2.pending_handoffs(run.id)
    assert len(ho) == 1 and ho[0].outcome_class is HandoffOutcome.UNCERTAIN
    assert disp2.get_run(run.id).routing_authority == LEAD_PENDING


def test_reconcile_is_idempotent_and_never_replays(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED, stop_after="start")
    first = disp.reconcile_on_restart(run.id)
    second = disp.reconcile_on_restart(run.id)
    assert len(first) == 1 and second == []  # no new handoffs, no replay


def test_reconcile_leaves_terminal_attempts_untouched(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED)
    assert disp.reconcile_on_restart(run.id) == []
    assert len(disp.pending_handoffs(run.id)) == 1  # only the real terminal handoff


# ─────────────────────────── at-least-once handoffs ───────────────────────────
def test_handoff_pending_until_acked_survives_crash(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED)
    disp.close()

    # Crash before Lead acknowledged: handoff must still be deliverable.
    disp2 = _open(tmp_path)
    pending = disp2.pending_handoffs(run.id)
    assert len(pending) == 1


def test_ack_is_idempotent(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED)
    hid = disp.pending_handoffs(run.id)[0].id

    disp.lead_decide(run.id, LeadDecision.dispatch("verifier", ack=(hid,)), configured_roles=ROLES)
    assert disp.pending_handoffs(run.id) == []
    # Re-acking the already-consumed handoff is a no-op, not an error.
    d3 = disp.lead_decide(run.id, LeadDecision.continue_lead(ack=(hid,)), configured_roles=ROLES)
    assert d3.dispatch is not None
    assert disp.pending_handoffs(run.id) == []


def test_ack_all_or_nothing_on_unknown_id(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.IDLE_COMPLETED)
    hid = disp.pending_handoffs(run.id)[0].id
    with pytest.raises(DecisionError):
        disp.lead_decide(
            run.id,
            LeadDecision.dispatch("verifier", ack=(hid, "ho-doesnotexist")),
            configured_roles=ROLES,
        )
    # Rolled back: valid handoff still pending, no new dispatch created.
    assert len(disp.pending_handoffs(run.id)) == 1
    assert len(disp.export_run(run.id)["attempts"]) == 1


# ─────────────────────────── thrash / no-progress bounds ───────────────────────────
def test_edge_repeat_guard_pauses_run(tmp_path):
    disp = _open(tmp_path, max_edge_repeats=2, max_consecutive_unproductive=0)
    run = disp.start_or_resume_run("goal:v1")
    # Two worker dispatches allowed; the third identical edge trips the guard.
    for _ in range(2):
        d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
        _drive(disp, run.id, d.dispatch.id, "worker", AttemptOutcome.SDK_ERROR)
    res = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    assert res.guard_tripped is True
    assert res.dispatch is None
    assert disp.get_run(run.id).status is RunStatus.PAUSED
    assert any(h.reason_returned == "thrash_guard" for h in disp.pending_handoffs(run.id))


def test_consecutive_unproductive_guard_pauses_run(tmp_path):
    disp = _open(tmp_path, max_consecutive_unproductive=2, max_edge_repeats=0)
    run = disp.start_or_resume_run("goal:v1")
    # Alternate roles so the edge-repeat guard is not what trips.
    for role in ("worker", "verifier"):
        d = disp.lead_decide(run.id, LeadDecision.dispatch(role), configured_roles=ROLES)
        _drive(disp, run.id, d.dispatch.id, role, AttemptOutcome.SDK_ERROR)
    res = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    assert res.guard_tripped is True
    assert disp.get_run(run.id).status is RunStatus.PAUSED


# ─────────────────────────── wait / pause / finish ───────────────────────────
def test_wait_persists_condition_launches_no_role(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    res = disp.lead_decide(run.id, LeadDecision.wait("verifier merges #15"), configured_roles=ROLES)
    got = disp.get_run(run.id)
    assert res.dispatch is not None and res.dispatch.kind is DecisionKind.WAIT
    assert got.status is RunStatus.WAITING
    assert got.wake_condition == "verifier merges #15"
    assert got.routing_authority == LEAD_PENDING


def test_finish_marks_run_finished(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    disp.lead_decide(run.id, LeadDecision.finish("all issues closed"), configured_roles=ROLES)
    assert disp.get_run(run.id).status is RunStatus.FINISHED


def test_pause_marks_run_paused_with_blocker(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    disp.lead_decide(run.id, LeadDecision.pause("needs human decision on API"), configured_roles=ROLES)
    got = disp.get_run(run.id)
    assert got.status is RunStatus.PAUSED
    assert got.human_blocker == "needs human decision on API"


def test_no_progress_override_counts_as_unproductive(tmp_path):
    disp = _open(tmp_path, max_consecutive_unproductive=5)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    # A completed SDK attempt that the role self-reports as no-progress.
    att = disp.reserve_attempt(run.id, d.dispatch.id, "worker")
    disp.mark_started(att, session_id="s", generation=0)
    disp.record_terminal(att, AttemptOutcome.IDLE_COMPLETED, outcome_class=HandoffOutcome.NO_PROGRESS)
    assert disp.get_run(run.id).consecutive_unproductive == 1
    assert disp.pending_handoffs(run.id)[0].outcome_class is HandoffOutcome.NO_PROGRESS
