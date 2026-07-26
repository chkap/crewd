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
    d1 = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    # First slot: crash right after reservation (before terminal).
    disp.reserve_attempt(run.id, d1.dispatch.id, "worker")
    disp.close()

    # Reopen: reserved slot persists, budget not refunded.
    disp2 = _open(tmp_path, max_work=2)
    assert disp2.get_run(run.id).reserved_slots == 1
    disp2.reconcile_on_restart(run.id)  # returns authority to Lead
    d2 = disp2.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    att2 = disp2.reserve_attempt(run.id, d2.dispatch.id, "worker")  # 2nd slot ok (==max)
    disp2.record_terminal(att2, AttemptOutcome.IDLE_COMPLETED)
    d3 = disp2.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    with pytest.raises(BudgetExhausted):
        disp2.reserve_attempt(run.id, d3.dispatch.id, "worker")  # 3rd over budget
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

    d2 = disp.lead_decide(run.id, LeadDecision.dispatch("verifier", ack=(hid,)), configured_roles=ROLES)
    assert disp.pending_handoffs(run.id) == []
    # Drive verifier to terminal so authority returns to Lead (new handoff hid2).
    _drive(disp, run.id, d2.dispatch.id, "verifier", AttemptOutcome.IDLE_COMPLETED)
    hid2 = disp.pending_handoffs(run.id)[0].id
    # Re-acking the already-consumed hid is a no-op; hid2 is consumed normally.
    disp.lead_decide(run.id, LeadDecision.continue_lead(ack=(hid, hid2)), configured_roles=ROLES)
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


# ─────────── run-lifecycle guards (Verifier #16 blocking finding) ───────────
def test_resume_does_not_create_second_active_run_after_pause(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    disp.lead_decide(r.id, LeadDecision.pause("human approval"), configured_roles=ROLES)
    r2 = disp.start_or_resume_run("goal:v1")
    # Same goal label must return the SAME durable (paused) run, not a fresh active one.
    assert r2.id == r.id
    assert r2.status is RunStatus.PAUSED


def test_resume_does_not_create_second_run_after_finish(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    disp.lead_decide(r.id, LeadDecision.finish("done"), configured_roles=ROLES)
    r2 = disp.start_or_resume_run("goal:v1")
    assert r2.id == r.id
    assert r2.status is RunStatus.FINISHED


def test_dispatch_from_paused_run_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    disp.lead_decide(r.id, LeadDecision.pause("blocker"), configured_roles=ROLES)
    with pytest.raises(DecisionError):
        disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    assert disp.get_run(r.id).status is RunStatus.PAUSED


def test_dispatch_from_finished_run_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    disp.lead_decide(r.id, LeadDecision.finish("done"), configured_roles=ROLES)
    with pytest.raises(DecisionError):
        disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)


def test_explicit_resume_reactivates_and_resets_thrash(tmp_path):
    disp = _open(tmp_path, max_edge_repeats=1, max_consecutive_unproductive=0)
    r = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    _drive(disp, r.id, d.dispatch.id, "worker", AttemptOutcome.SDK_ERROR)
    res = disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    assert res.guard_tripped and disp.get_run(r.id).status is RunStatus.PAUSED
    resumed = disp.resume_run(r.id)
    assert resumed.status is RunStatus.ACTIVE
    assert resumed.routing_authority == LEAD_PENDING
    assert resumed.last_edge_repeats == 0
    # After resume, Lead may dispatch again.
    d2 = disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    assert d2.dispatch is not None


def test_resume_finished_run_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    disp.lead_decide(r.id, LeadDecision.finish("done"), configured_roles=ROLES)
    with pytest.raises(DecisionError):
        disp.resume_run(r.id)


# ─────────── exclusive authority + attempt binding (Advisory finding) ───────────
def test_overlapping_lead_decision_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    # Authority is held by the first dispatch; a second decision must be refused.
    with pytest.raises(DecisionError):
        disp.lead_decide(r.id, LeadDecision.dispatch("verifier"), configured_roles=ROLES)
    # No second dispatch row created.
    assert len(disp.export_run(r.id)["attempts"]) == 0


def test_duplicate_reservation_per_dispatch_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    disp.reserve_attempt(r.id, d.dispatch.id, "worker")
    before = disp.get_run(r.id).reserved_slots
    with pytest.raises(DecisionError):
        disp.reserve_attempt(r.id, d.dispatch.id, "worker")
    # No extra slot consumed on the rejected duplicate.
    assert disp.get_run(r.id).reserved_slots == before


def test_wrong_role_reservation_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    with pytest.raises(DecisionError):
        disp.reserve_attempt(r.id, d.dispatch.id, "verifier")
    assert disp.get_run(r.id).reserved_slots == 0


def test_cross_run_dispatch_reservation_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r1 = disp.start_or_resume_run("goal:v1")
    d1 = disp.lead_decide(r1.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    r2 = disp.start_or_resume_run("goal:v2")
    # Reserving an attempt in r2 that points at r1's dispatch must be refused.
    with pytest.raises(DecisionError):
        disp.reserve_attempt(r2.id, d1.dispatch.id, "worker")
    assert disp.get_run(r2.id).reserved_slots == 0


def test_reservation_without_authority_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(r.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    att = disp.reserve_attempt(r.id, d.dispatch.id, "worker")
    disp.record_terminal(att, AttemptOutcome.IDLE_COMPLETED)  # authority back to Lead
    # The old dispatch no longer owns authority; a late reservation is refused.
    with pytest.raises(DecisionError):
        disp.reserve_attempt(r.id, d.dispatch.id, "worker")


def test_reserve_on_non_routing_dispatch_is_rejected(tmp_path):
    disp = _open(tmp_path)
    r = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(r.id, LeadDecision.wait("some condition"), configured_roles=ROLES)
    # WAIT is recorded as a dispatch row but is not a routing decision.
    with pytest.raises(DecisionError):
        disp.reserve_attempt(r.id, d.dispatch.id, "lead")


# ─────────────────────── journaled Lead solicitation (#17) ───────────────────────
def _seed_pending(disp, run_id, role="worker", **ho):
    """Dispatch a role and complete it so exactly one pending handoff exists."""
    d = disp.lead_decide(run_id, LeadDecision.dispatch(role), configured_roles=ROLES)
    _drive(disp, run_id, d.dispatch.id, role, AttemptOutcome.IDLE_COMPLETED, **ho)
    return disp.pending_handoffs(run_id)[-1]


def _last_solicit_attempt(disp, run_id):
    row = disp._conn.execute(
        "SELECT attempt_id FROM solicitation WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return row["attempt_id"]


def test_solicitation_consumes_budget_and_owns_authority(tmp_path):
    disp = _open(tmp_path, max_work=3)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    got = disp.get_run(run.id)
    # A journaled Lead turn reserves one work slot and takes routing authority.
    assert got.reserved_slots == 1
    assert got.routing_authority == sol.dispatch_id
    assert disp.get_attempt(sol.attempt_id).role == "lead"
    assert disp.get_attempt(sol.attempt_id).state is AttemptState.RESERVED


def test_solicitation_budget_exhaustion(tmp_path):
    disp = _open(tmp_path, max_work=1)
    run = disp.start_or_resume_run("goal:v1")
    disp.open_lead_solicitation(run.id)  # consumes the only slot
    # Resolve it to return authority to Lead so a second open passes the authority gate.
    disp.resolve_lead_solicitation(
        _last_solicit_attempt(disp, run.id),
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.continue_lead(),
        configured_roles=ROLES,
    )
    with pytest.raises(BudgetExhausted):
        disp.open_lead_solicitation(run.id)
    assert disp.get_run(run.id).status is RunStatus.EXHAUSTED


def test_solicitation_open_requires_active_lead_pending(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    # Authority is held by an in-flight role dispatch → cannot solicit.
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    with pytest.raises(DecisionError):
        disp.open_lead_solicitation(run.id)
    disp.record_terminal(disp.reserve_attempt(run.id, d.dispatch.id, "worker"),
                         AttemptOutcome.IDLE_COMPLETED)
    # Paused run cannot be solicited either.
    disp.lead_decide(run.id, LeadDecision.pause("blocked",
                     ack=(disp.pending_handoffs(run.id)[0].id,)), configured_roles=ROLES)
    with pytest.raises(DecisionError):
        disp.open_lead_solicitation(run.id)


def test_solicitation_apply_dispatch_no_handoff_emitted(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    ho = _seed_pending(disp, run.id, evidence="did work")
    before = len(disp.export_run(run.id)["handoffs"])
    sol = disp.open_lead_solicitation(run.id)
    assert set(sol.pending_handoff_ids) == {ho.id}
    disp.mark_started(sol.attempt_id, session_id="lead-sess", generation=0)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.dispatch("verifier", ack=(ho.id,)),
        configured_roles=ROLES,
    )
    assert res.dispatch.role == "verifier"
    assert disp.get_run(run.id).routing_authority == res.dispatch.id
    # The Lead attempt terminalised but emitted NO handoff of its own.
    assert disp.get_attempt(sol.attempt_id).state is AttemptState.TERMINAL
    assert len(disp.export_run(run.id)["handoffs"]) == before
    assert disp.pending_handoffs(run.id) == []  # snapshot handoff was acked


def test_solicitation_requires_exact_ack_snapshot(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    ho = _seed_pending(disp, run.id)
    sol = disp.open_lead_solicitation(run.id)
    # Ack the wrong (empty) set → invalid; handoff stays pending, authority to Lead.
    res = disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.dispatch("verifier", ack=()),
        configured_roles=ROLES,
    )
    assert res.solicitation_invalid
    assert disp.get_run(run.id).routing_authority == LEAD_PENDING
    assert [h.id for h in disp.pending_handoffs(run.id)] == [ho.id]
    assert disp.get_run(run.id).invalid_solicitations == 1


def test_solicitation_no_decision_is_invalid(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id, outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=None, configured_roles=ROLES,
    )
    assert res.solicitation_invalid
    assert disp.get_run(run.id).invalid_solicitations == 1
    assert disp.get_run(run.id).routing_authority == LEAD_PENDING


def test_solicitation_unclean_outcome_is_invalid(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id, outcome=AttemptOutcome.SDK_ERROR,
        decision=LeadDecision.continue_lead(), configured_roles=ROLES,
    )
    assert res.solicitation_invalid
    assert disp.get_attempt(sol.attempt_id).terminal_outcome is AttemptOutcome.SDK_ERROR


def test_solicitation_invalid_cap_pauses(tmp_path):
    disp = _open(tmp_path, max_invalid_solicitations=2)
    run = disp.start_or_resume_run("goal:v1")
    for _ in range(2):
        sol = disp.open_lead_solicitation(run.id)
        disp.resolve_lead_solicitation(
            sol.attempt_id, outcome=AttemptOutcome.IDLE_COMPLETED,
            decision=None, configured_roles=ROLES,
        )
    got = disp.get_run(run.id)
    assert got.status is RunStatus.PAUSED
    assert got.invalid_solicitations == 2
    assert got.human_blocker


def test_valid_solicitation_resets_invalid_counter(tmp_path):
    disp = _open(tmp_path, max_invalid_solicitations=5)
    run = disp.start_or_resume_run("goal:v1")
    sol1 = disp.open_lead_solicitation(run.id)
    disp.resolve_lead_solicitation(sol1.attempt_id, outcome=AttemptOutcome.IDLE_COMPLETED,
                                   decision=None, configured_roles=ROLES)
    assert disp.get_run(run.id).invalid_solicitations == 1
    sol2 = disp.open_lead_solicitation(run.id)
    disp.resolve_lead_solicitation(sol2.attempt_id, outcome=AttemptOutcome.IDLE_COMPLETED,
                                   decision=LeadDecision.dispatch("worker", ack=()),
                                   configured_roles=ROLES)
    assert disp.get_run(run.id).invalid_solicitations == 0


def test_solicitation_continue_lead_returns_to_lead(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id, outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.continue_lead(), configured_roles=ROLES,
    )
    # continue_lead grants no free recursion: authority returns to Lead for
    # another (budgeted) solicitation.
    assert not res.solicitation_invalid
    assert disp.get_run(run.id).routing_authority == LEAD_PENDING
    assert disp.get_run(run.id).status is RunStatus.ACTIVE


def test_solicit_lead_is_not_a_valid_decision(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    bad = LeadDecision(DecisionKind.SOLICIT_LEAD)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id, outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=bad, configured_roles=ROLES,
    )
    assert res.solicitation_invalid


def test_resolve_non_solicitation_attempt_raises(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    att = disp.reserve_attempt(run.id, d.dispatch.id, "worker")
    with pytest.raises(DecisionError):
        disp.resolve_lead_solicitation(att, outcome=AttemptOutcome.IDLE_COMPLETED,
                                       decision=LeadDecision.continue_lead(),
                                       configured_roles=ROLES)


def test_restart_reconciles_solicitation_without_handoff(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    disp.mark_started(sol.attempt_id, session_id="lead-sess", generation=0)
    nonce_before = disp.get_run(run.id).authority_seq
    disp.close()

    # Crash between the Lead turn and resolution: the in-memory candidate is lost.
    disp2 = _open(tmp_path)
    created = disp2.reconcile_on_restart(run.id)
    # A solicitation reconcile emits NO handoff (Lead attempts never do)…
    assert created == []
    assert disp2.pending_handoffs(run.id) == []
    assert disp2.get_attempt(sol.attempt_id).state is AttemptState.RECONCILED_UNCERTAIN
    # …but authority returns to Lead with a bumped nonce, so any late candidate
    # for the dead solicitation can never apply.
    got = disp2.get_run(run.id)
    assert got.routing_authority == LEAD_PENDING
    assert got.authority_seq > nonce_before


def test_stale_solicitation_resolve_is_rejected_after_restart(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    disp.mark_started(sol.attempt_id, session_id="lead-sess", generation=0)
    disp.close()

    disp2 = _open(tmp_path)
    disp2.reconcile_on_restart(run.id)
    # Resolving the reconciled (no longer in-flight) solicitation must be refused.
    with pytest.raises(DecisionError):
        disp2.resolve_lead_solicitation(
            sol.attempt_id, outcome=AttemptOutcome.IDLE_COMPLETED,
            decision=LeadDecision.continue_lead(), configured_roles=ROLES,
        )


def test_solicitation_wait_sets_status(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run("goal:v1")
    sol = disp.open_lead_solicitation(run.id)
    disp.resolve_lead_solicitation(
        sol.attempt_id, outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.wait("upstream merge"), configured_roles=ROLES,
    )
    got = disp.get_run(run.id)
    assert got.status is RunStatus.WAITING
    assert got.routing_authority == LEAD_PENDING
    assert got.wake_condition == "upstream merge"
