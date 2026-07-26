"""Operator diagnostic surface (#13).

These tests drive a *real* :class:`~crewd.dispatcher.Dispatcher` into each durable
run state, persist it, then reopen and project it with
:func:`crewd.diagnostics.build_snapshot`. The point is that the recommended
``safe_next_action`` is a finite, deterministic function of persisted state +
lower-authority controls — so every branch is exercised from committed fixtures,
not mocked internals.
"""
from __future__ import annotations

import os

import pytest

from crewd.config import CrewConfig, GoalState
from crewd.diagnostics import NextAction, build_snapshot
from crewd.dispatcher import (
    AttemptState,
    Dispatcher,
    HandoffOutcome,
    LeadDecision,
    RunStatus,
    LEAD_PENDING,
)
from crewd.session_backend import AttemptOutcome, TaintStore

ROLES = ("lead", "advisory", "worker", "verifier")
GOAL = "goal:v1"


# ─────────────────────────── fixtures / helpers ───────────────────────────
def _label(ws) -> None:
    """Persist a goal.json so the workspace has a goal label."""
    GoalState(version=1, goal_md_sha256="x", label=GOAL, cycles=0).save(ws.goal_json)


def _disp(ws) -> Dispatcher:
    ws.state_dir.mkdir(parents=True, exist_ok=True)
    return Dispatcher(ws.state_dir / "dispatch.db")


def _snap(ws):
    cfg = CrewConfig.load(ws.crew_yaml)
    return build_snapshot(ws, crew_name=cfg.name, backend=cfg.backend, goal_label=GOAL)


def _dispatch(disp, run_id, role="worker", **ack):
    return disp.lead_decide(
        run_id, LeadDecision.dispatch(role, **ack), configured_roles=ROLES
    )


# ─────────────────────────── no journal ───────────────────────────
def test_no_journal_recommends_run(tmp_ws):
    _label(tmp_ws)
    snap = _snap(tmp_ws)
    assert snap.run_id is None
    assert snap.next_action is NextAction.NO_JOURNAL


def test_no_goal_label_is_no_journal(tmp_ws):
    # dispatch.db exists but there's no goal.json → no label → nothing to read.
    disp = _disp(tmp_ws)
    disp.start_or_resume_run(GOAL)
    disp.close()
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    snap = build_snapshot(
        tmp_ws, crew_name=cfg.name, backend=cfg.backend, goal_label=None
    )
    assert snap.next_action is NextAction.NO_JOURNAL


# ─────────────────────────── active states ───────────────────────────
def test_active_idle_recommends_continue(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    disp.start_or_resume_run(GOAL)  # authority = lead_pending, no attempt
    disp.close()
    snap = _snap(tmp_ws)
    assert snap.run_status == "active"
    assert snap.routing_authority == LEAD_PENDING
    assert snap.authority_holder == "lead"
    assert snap.current_attempt is None
    assert snap.next_action is NextAction.CONTINUE


def test_active_live_daemon_lead_authority_is_running(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    disp.start_or_resume_run(GOAL)
    disp.close()
    tmp_ws.write_pid(os.getpid())  # alive daemon
    try:
        snap = _snap(tmp_ws)
        assert snap.daemon_alive is True
        assert snap.next_action is NextAction.RUNNING
    finally:
        tmp_ws.clear_pid()


def test_inflight_attempt_live_daemon_is_running(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    d = _dispatch(disp, run.id)
    att = disp.reserve_attempt(run.id, d.dispatch.id, "worker")
    disp.mark_started(att, session_id="sess-1", generation=0)
    disp.close()
    tmp_ws.write_pid(os.getpid())
    try:
        snap = _snap(tmp_ws)
        assert snap.current_attempt is not None
        assert snap.current_attempt["role"] == "worker"
        assert snap.current_attempt["state"] == AttemptState.STARTED.value
        assert snap.next_action is NextAction.RUNNING
    finally:
        tmp_ws.clear_pid()


def test_orphaned_started_attempt_dead_daemon_is_resume_orphan(tmp_ws):
    """A crash leaves a `started` attempt AND authority still on the dispatch id."""
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    d = _dispatch(disp, run.id)
    att = disp.reserve_attempt(run.id, d.dispatch.id, "worker")
    disp.mark_started(att, session_id="sess-orphan", generation=0)
    disp.close()  # crash: no record_terminal, no daemon
    snap = _snap(tmp_ws)
    assert snap.daemon_alive is False
    assert snap.current_attempt is not None
    assert snap.current_attempt["session_id"] == "sess-orphan"
    assert snap.next_action is NextAction.RESUME_ORPHAN
    assert "resume" in snap.next_action_detail.lower()


def test_orphan_with_tainted_session_flags_fresh_generation(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    d = _dispatch(disp, run.id)
    att = disp.reserve_attempt(run.id, d.dispatch.id, "worker")
    disp.mark_started(att, session_id="sess-tainted", generation=0)
    disp.close()
    # taint the orphan's session for the worker role
    TaintStore(tmp_ws.role_cfg_dir("worker") / ".crewd-sdk-taint").taint("sess-tainted")
    snap = _snap(tmp_ws)
    assert snap.current_session_tainted is True
    assert snap.current_attempt["tainted"] is True
    assert snap.next_action is NextAction.RESUME_ORPHAN
    assert "fresh generation" in snap.next_action_detail


# ─────────────────────────── non-active durable states ───────────────────────────
def test_waiting_recommends_wait(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    disp.lead_decide(run.id, LeadDecision.wait("CI to go green"), configured_roles=ROLES)
    disp.close()
    snap = _snap(tmp_ws)
    assert snap.run_status == "waiting"
    assert snap.wake_condition == "CI to go green"
    assert snap.next_action is NextAction.WAIT


def test_paused_recommends_resolve_blocker(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    disp.lead_decide(run.id, LeadDecision.pause("need human decision"), configured_roles=ROLES)
    disp.close()
    snap = _snap(tmp_ws)
    assert snap.run_status == "paused"
    assert snap.human_blocker == "need human decision"
    assert snap.next_action is NextAction.RESOLVE_BLOCKER


def test_interrupted_recommends_resume(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    disp.mark_run_status(run.id, RunStatus.INTERRUPTED)
    disp.close()
    snap = _snap(tmp_ws)
    assert snap.run_status == "interrupted"
    assert snap.next_action is NextAction.RESUME


def test_stopped_run_recommends_resume(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    disp.mark_run_status(run.id, RunStatus.STOPPED)
    disp.close()
    snap = _snap(tmp_ws)
    assert snap.run_status == "stopped"
    assert snap.next_action is NextAction.RESUME


def test_finished_recommends_new_goal(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    disp.lead_decide(run.id, LeadDecision.finish("all acceptance met"), configured_roles=ROLES)
    disp.close()
    snap = _snap(tmp_ws)
    assert snap.run_status == "finished"
    assert snap.next_action is NextAction.NEW_GOAL


def test_exhausted_recommends_new_goal(tmp_ws):
    _label(tmp_ws)
    from crewd.dispatcher import DispatcherLimits, BudgetExhausted
    tmp_ws.state_dir.mkdir(parents=True, exist_ok=True)
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db", DispatcherLimits(max_work=1))
    run = disp.start_or_resume_run(GOAL)
    d = _dispatch(disp, run.id)
    att = disp.reserve_attempt(run.id, d.dispatch.id, "worker")  # consumes the only slot
    disp.mark_started(att, session_id="s", generation=0)
    disp.record_terminal(att, AttemptOutcome.IDLE_COMPLETED, evidence="e", changed="none")
    d2 = _dispatch(disp, run.id)
    with pytest.raises(BudgetExhausted):
        disp.reserve_attempt(run.id, d2.dispatch.id, "worker")
    disp.close()
    snap = _snap(tmp_ws)
    assert snap.run_status == "exhausted"
    assert snap.next_action is NextAction.NEW_GOAL


# ─────────────────────────── contradictions → doctor ───────────────────────────
def test_stopped_sentinel_with_live_daemon_is_contradiction(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    disp.start_or_resume_run(GOAL)  # active
    disp.close()
    tmp_ws.stop("manual")           # STOPPED sentinel
    tmp_ws.write_pid(os.getpid())   # but daemon still alive
    try:
        snap = _snap(tmp_ws)
        assert snap.contradictions
        assert snap.next_action is NextAction.DOCTOR
    finally:
        tmp_ws.clear_pid()


def test_stale_exit_reason_while_active_is_contradiction(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    disp.start_or_resume_run(GOAL)  # durably active
    disp.close()
    tmp_ws.exit_reason_file.write_text("goal-complete\n")
    snap = _snap(tmp_ws)
    assert any("stale" in c for c in snap.contradictions)
    assert snap.next_action is NextAction.DOCTOR


def test_live_daemon_on_finished_run_is_contradiction(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    disp.lead_decide(run.id, LeadDecision.finish("done"), configured_roles=ROLES)
    disp.close()
    tmp_ws.write_pid(os.getpid())
    try:
        snap = _snap(tmp_ws)
        assert snap.contradictions
        assert snap.next_action is NextAction.DOCTOR
    finally:
        tmp_ws.clear_pid()


# ─────────────────────────── redaction / bounding ───────────────────────────
def test_handoff_summary_redacts_and_bounds(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    run = disp.start_or_resume_run(GOAL)
    d = _dispatch(disp, run.id)
    att = disp.reserve_attempt(run.id, d.dispatch.id, "worker")
    disp.mark_started(att, session_id="s", generation=0)
    secret = "token=ghp_" + "A" * 30
    disp.record_terminal(
        att, AttemptOutcome.IDLE_COMPLETED,
        evidence="x" * 5000,                       # oversized
        changed="none",
        remaining="see " + secret,                 # secret in free text
        reason_returned="auth Bearer sk-topsecretvalue done",
    )
    disp.close()
    snap = _snap(tmp_ws)
    h = snap.latest_handoff
    assert h is not None
    # default view: presence + length only, no raw text
    assert h["evidence"] == {"present": True, "chars": 5000}
    assert "text" not in h["evidence"]
    # reason is always redacted + bounded, never leaks the bearer token
    assert "sk-topsecretvalue" not in h["reason"]
    assert "«redacted»" in h["reason"]


def test_json_snapshot_is_stable(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    disp.start_or_resume_run(GOAL)
    disp.close()
    snap = _snap(tmp_ws)
    d = snap.to_dict()
    for key in ("workspace_root", "crew_name", "backend", "goal_label",
                "run", "current_attempt", "latest_handoff", "latest_decision",
                "controls", "contradictions", "next_action", "next_action_detail"):
        assert key in d
    assert d["next_action"] == snap.next_action.value
    assert set(d["controls"]) == {
        "daemon_pid", "daemon_alive", "stopped", "paused_reason",
        "exit_reason", "current_session_tainted",
    }


# ─────────────────────────── read-only guarantee ───────────────────────────
def test_build_snapshot_does_not_clear_stale_pid(tmp_ws):
    _label(tmp_ws)
    disp = _disp(tmp_ws)
    disp.start_or_resume_run(GOAL)
    disp.close()
    tmp_ws.write_pid(999999)  # almost certainly not alive → stale
    assert tmp_ws.read_pid() == 999999
    snap = _snap(tmp_ws)
    assert snap.daemon_alive is False
    # read-only: the stale PID file must survive a status projection
    assert tmp_ws.read_pid() == 999999
