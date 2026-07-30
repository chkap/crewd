"""Intent-aware typed gate corrections (issue #64).

Proves the core of Advisory option 2: explicit, durable dispatch intent and
typed, non-mutating gate corrections returned to Lead before authority changes.
Covers implementation review vs Lead-assigned verifier-only audit/acceptance,
conservative legacy/untyped defaults, the exact-binding + non-mutation
invariants (#47/#49), idempotent migration compatibility, and the incident-
derived regressions the goal calls out (#61 direct Verifier audit, missing
assignment/readiness, stale/unrelated closure, bounded escalation).

Deterministic: the fake executor + fake GitHub boundary, no SDK or network.
"""
from __future__ import annotations

import sqlite3

from crewd.config import CrewConfig, GoalState
from crewd.dispatcher import (
    Dispatcher,
    DispatcherLimits,
    DispatchIntent,
    LeadDecision,
    RunStatus,
)
from crewd.executor import parse_lead_decision
from crewd.github_bus import (
    GateCorrection,
    PublicBus,
    PublicBusGate,
    RejectReason,
    Route,
)
from crewd.orchestrator import Orchestrator
from crewd.workspace import Workspace

from fakes import FakeExecutor, dispatch_to, wait
from fake_github import FakeGitHubClient

CREW = "testcrew"
REPO = "acme/widget"
GOAL = "goal:v2"
TASK = 29


def _bus(client: FakeGitHubClient) -> PublicBus:
    return PublicBus(client, crew=CREW, expected_repo=REPO, goal_label=GOAL)


def _orch(
    tmp_ws: Workspace, fake: FakeExecutor, gate, *, limits=None
) -> tuple[Orchestrator, Dispatcher]:
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = 0
    gs = GoalState(version=1, label=GOAL, cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db", limits=limits)
    orch = Orchestrator(tmp_ws, cfg, fake, gs, dispatcher=disp, bus_gate=gate, max_steps=50)
    return orch, disp


def _assign_worker(c: FakeGitHubClient, task: int) -> None:
    c.add_comment("issue", task, f"> **[crewd:lead -> worker]** {CREW}\n\nAssigned.")


def _assign_verifier(c: FakeGitHubClient, task: int) -> None:
    c.add_comment("issue", task, f"> **[crewd:lead -> verifier]** {CREW}\n\nAudit this.")


# ── DispatchIntent coercion (conservative default) ──
def test_intent_coerce_defaults_to_implementation():
    assert DispatchIntent.coerce(None) is DispatchIntent.IMPLEMENTATION
    assert DispatchIntent.coerce("") is DispatchIntent.IMPLEMENTATION
    assert DispatchIntent.coerce("bogus") is DispatchIntent.IMPLEMENTATION
    assert DispatchIntent.coerce("ACCEPTANCE") is DispatchIntent.ACCEPTANCE
    assert DispatchIntent.coerce("verifier_audit") is DispatchIntent.VERIFIER_AUDIT
    assert DispatchIntent.coerce(DispatchIntent.RELEASE) is DispatchIntent.RELEASE


def test_lead_decision_dispatch_carries_intent():
    d = LeadDecision.dispatch("verifier", intent="acceptance", task_number=7)
    assert d.intent is DispatchIntent.ACCEPTANCE
    # Omitted intent → conservative IMPLEMENTATION.
    assert LeadDecision.dispatch("worker").intent is DispatchIntent.IMPLEMENTATION


def test_parse_lead_decision_reads_intent():
    d = parse_lead_decision(
        {"kind": "dispatch", "role": "verifier", "task_number": 12,
         "intent": "release", "ack_handoff_ids": []}
    )
    assert d.intent is DispatchIntent.RELEASE
    # A dispatch that omits intent is IMPLEMENTATION (strongest safeguards).
    d2 = parse_lead_decision({"kind": "dispatch", "role": "worker", "ack_handoff_ids": []})
    assert d2.intent is DispatchIntent.IMPLEMENTATION


# ── intent-aware gate predicates ──
def _open_owned_task(c: FakeGitHubClient) -> None:
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL), assignees=("alice",))


def test_verifier_audit_needs_no_pr_or_readiness():
    """The #61 incident: a Lead-assigned verifier-only audit must dispatch on an
    open, owned, goal-linked task WITHOUT a linked PR or Worker readiness."""
    c = FakeGitHubClient(REPO)
    _open_owned_task(c)  # no PR, no readiness
    gate = PublicBusGate(_bus(c), task_number=TASK)

    audit = gate.evaluate("verifier", None, intent=DispatchIntent.VERIFIER_AUDIT)
    assert audit.route is Route.PROCEED
    # The same record under an implementation review is (correctly) rejected —
    # no PR / readiness — proving the audit path is a real relaxation, not a hole.
    impl = gate.evaluate("verifier", None, intent=DispatchIntent.IMPLEMENTATION)
    assert impl.route is Route.REJECT
    assert impl.reason is RejectReason.MISSING


def test_acceptance_and_release_intents_use_audit_predicate():
    c = FakeGitHubClient(REPO)
    _open_owned_task(c)
    gate = PublicBusGate(_bus(c), task_number=TASK)
    for intent in ("acceptance", "release", "advisory"):
        assert gate.evaluate("verifier", None, intent=intent).route is Route.PROCEED


def test_verifier_audit_still_enforces_ownership():
    """A verifier-only intent relaxes PR/readiness but NOT the pre-dispatch
    safety of an open, owned, goal-linked task."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))  # no assignee, no assignment
    gate = PublicBusGate(_bus(c), task_number=TASK)
    out = gate.evaluate("verifier", None, intent=DispatchIntent.VERIFIER_AUDIT)
    assert out.route is Route.REJECT
    assert out.reason is RejectReason.NO_ASSIGNMENT


def test_verifier_audit_accepts_verifier_assignment():
    """Ownership for a verifier-only task may be a public Lead→verifier
    assignment (there is no Worker to assign)."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))
    _assign_verifier(c, TASK)
    gate = PublicBusGate(_bus(c), task_number=TASK)
    assert gate.evaluate("verifier", None, intent=DispatchIntent.VERIFIER_AUDIT).route is Route.PROCEED


def test_legacy_untyped_verifier_dispatch_keeps_full_safeguards():
    """A dispatch with no intent (legacy/untyped) reads as IMPLEMENTATION, so the
    linked-PR + readiness gate still applies — a missing record is not bypassed."""
    c = FakeGitHubClient(REPO)
    _open_owned_task(c)
    gate = PublicBusGate(_bus(c), task_number=TASK)
    out = gate.evaluate("verifier", None, intent=None)  # untyped
    assert out.route is Route.REJECT


# ── typed correction structure ──
def test_build_correction_fields_for_missing_assignment():
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))  # no assignment
    gate = PublicBusGate(_bus(c), task_number=TASK)
    outcome = gate.evaluate("worker", None, intent=DispatchIntent.IMPLEMENTATION)
    assert outcome.route is Route.REJECT
    corr = gate.build_correction(
        "worker", DispatchIntent.IMPLEMENTATION, outcome, task_number=TASK
    )
    assert isinstance(corr, GateCorrection)
    assert corr.repo == REPO and corr.goal == GOAL
    assert corr.role == "worker" and corr.intent == "implementation"
    assert corr.task == TASK
    assert corr.failed_predicate == RejectReason.NO_ASSIGNMENT.value
    assert corr.retry_class == "correctable"
    assert "reroute" in corr.allowed_lead_actions
    # Round-trips to JSON for the durable record / Lead prompt.
    import json
    parsed = json.loads(corr.to_json())
    assert parsed["failed_predicate"] == "no_assignment"
    assert parsed["retry_class"] == "correctable"


def test_wrong_repo_is_operator_not_correctable():
    """A wrong-repo (workspace/config) rejection is an operator prerequisite, not
    a Lead-correctable public-record inconsistency."""
    assert RejectReason.WRONG_REPO.lead_correctable is False
    assert RejectReason.NO_ASSIGNMENT.lead_correctable is True
    assert RejectReason.NOT_READY.lead_correctable is True

    c = FakeGitHubClient("other/repo")  # client repo != expected
    gate = PublicBusGate(_bus(c), task_number=TASK)
    outcome = gate.evaluate("worker", None)
    assert outcome.route is Route.REJECT and outcome.reason is RejectReason.WRONG_REPO
    corr = gate.build_correction("worker", None, outcome, task_number=TASK)
    assert corr.retry_class == "operator"
    assert corr.allowed_lead_actions == ("escalate",)


# ── orchestrator: correctable rejection routed to Lead, no mutation ──
def test_correctable_rejection_returns_to_lead_without_mutation(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))  # no assignment → correctable
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[dispatch_to("worker", task_number=TASK)])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=True)

    run = disp.start_or_resume_run(GOAL)
    # Not paused; authority returned to Lead; no attempt reserved; correction set.
    assert RunStatus(run.status) is RunStatus.ACTIVE
    assert run.routing_authority == "lead_pending"
    assert run.consecutive_corrections == 1
    assert fake.role_calls == []
    assert "no_assignment" in (run.gate_correction or "")
    # The correction is injected into the next Lead prompt.
    prompt = orch._lead_prompt([], run.id)
    assert "PUBLIC-BUS GATE CORRECTION" in prompt
    assert "no_assignment" in prompt


def test_non_clean_lead_turn_is_not_downgraded_to_correction(tmp_ws: Workspace):
    """A correction only applies when the Lead turn was well-formed. A turn that
    ended non-cleanly (SDK error/taint/cancel) may still carry a captured
    candidate dispatch that would fail the gate predicate, but it must NOT be
    recorded as a benign correction — that would let a failed Lead turn loop
    unbounded by the correction path. It falls through to the invalid/failed
    solicitation accounting instead (no correction recorded)."""
    from crewd.session_backend import AttemptOutcome

    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))  # would be correctable
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(
        lead_script=[dispatch_to("worker", task_number=TASK)],
        lead_outcome=AttemptOutcome.SDK_ERROR,
    )
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=True)

    run = disp.start_or_resume_run(GOAL)
    # The non-clean turn is not counted as a correction and never dispatches.
    assert run.consecutive_corrections == 0
    assert run.gate_correction is None
    assert fake.role_calls == []


def test_direct_verifier_audit_dispatches_without_worker_work(tmp_ws: Workspace):
    """#61: a Lead-assigned verifier-only audit dispatches directly with no fake
    Worker PR / readiness — the whole point of intent-aware gating."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))
    _assign_verifier(c, TASK)
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[
        dispatch_to("verifier", task_number=TASK, intent="verifier_audit"),
        wait("external"),
    ])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    assert [r.role for r in fake.role_calls] == ["verifier"]


def test_implementation_verifier_still_requires_pr(tmp_ws: Workspace):
    """A normal implementation-review Verifier dispatch keeps the linked-PR +
    readiness safeguards — a missing record is a correctable rejection, not a
    silent pass."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))
    _assign_worker(c, TASK)  # owned, but no PR / readiness
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[
        dispatch_to("verifier", task_number=TASK, intent="implementation"),
    ])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=True)

    assert fake.role_calls == []
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.ACTIVE  # correctable, not paused
    assert run.gate_correction is not None


def test_productive_decision_clears_correction_streak(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))  # correctable at first
    gate = PublicBusGate(_bus(c), task_number=TASK)

    fake = FakeExecutor(lead_script=[
        dispatch_to("worker", task_number=TASK),  # correctable → correction
        wait("waiting on operator"),               # productive → clears streak
    ])
    orch, disp = _orch(tmp_ws, fake, gate)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert run.consecutive_corrections == 0
    assert run.gate_correction is None
    assert RunStatus(run.status) is RunStatus.WAITING


def test_bounded_correction_streak_settles_to_waiting(tmp_ws: Workspace):
    """Recovery is bounded but a repeated correctable public-record inconsistency
    is NOT a human blocker (GOAL.md): after ``max_gate_corrections`` consecutive
    uncorrected rejections the run settles into a recoverable WAITING state with a
    wake condition — never a human PAUSE — rather than looping forever."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))  # persistently unassigned
    gate = PublicBusGate(_bus(c), task_number=TASK)

    limits = DispatcherLimits(max_gate_corrections=2)
    fake = FakeExecutor(lead_script=[
        dispatch_to("worker", task_number=TASK),
        dispatch_to("worker", task_number=TASK),
        dispatch_to("worker", task_number=TASK),
    ])
    orch, disp = _orch(tmp_ws, fake, gate, limits=limits)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.WAITING
    assert run.human_blocker is None  # a repeated correction is not a human block
    assert "consecutive uncorrected gate rejections" in (run.wake_condition or "")
    assert fake.role_calls == []  # never dispatched a worker


# ── durable intent + migration compatibility ──
def test_intent_persisted_and_read_back(tmp_ws: Workspace):
    from crewd.config import ROLES
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    run = disp.start_or_resume_run(GOAL)
    res = disp.lead_decide(
        run.id,
        LeadDecision.dispatch("verifier", task_number=TASK, intent="acceptance"),
        configured_roles=ROLES,
    )
    dsp = disp.get_dispatch(res.dispatch.id)
    assert dsp.intent is DispatchIntent.ACCEPTANCE
    assert dsp.task_number == TASK


def test_migration_from_pre64_db_defaults_to_implementation(tmp_ws: Workspace):
    """A dispatch row written by a pre-#64 kernel (no ``intent`` column) reads as
    IMPLEMENTATION — the conservative default — after idempotent migration, and
    the migration is safe to run repeatedly."""
    db_path = tmp_ws.state_dir / "dispatch.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Hand-build a minimal pre-#64 schema (dispatch has no intent column, goal_run
    # has no correction columns) and seed one legacy dispatch row.
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE goal_run (
            id TEXT PRIMARY KEY, goal_label TEXT NOT NULL, status TEXT NOT NULL,
            routing_authority TEXT NOT NULL, reserved_slots INTEGER NOT NULL DEFAULT 0,
            consecutive_unproductive INTEGER NOT NULL DEFAULT 0, last_edge TEXT,
            last_edge_repeats INTEGER NOT NULL DEFAULT 0, wake_condition TEXT,
            human_blocker TEXT, authority_seq INTEGER NOT NULL DEFAULT 0,
            invalid_solicitations INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE dispatch (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
            kind TEXT NOT NULL, role TEXT, reason TEXT, task_number INTEGER,
            pr_number INTEGER, created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO goal_run (id, goal_label, status, routing_authority, created_at) "
        "VALUES ('run-legacy', ?, 'active', 'lead_pending', 't')", (GOAL,)
    )
    conn.execute(
        "INSERT INTO dispatch (id, run_id, seq, kind, role, task_number, created_at) "
        "VALUES ('dsp-legacy', 'run-legacy', 1, 'dispatch', 'worker', 42, 't')"
    )
    conn.commit()
    conn.close()

    # Opening with the current kernel migrates in place (idempotently).
    disp = Dispatcher(db_path)
    dsp = disp.get_dispatch("dsp-legacy")
    assert dsp.intent is DispatchIntent.IMPLEMENTATION  # conservative default
    assert dsp.task_number == 42
    run = disp.get_run("run-legacy")
    assert run.consecutive_corrections == 0
    assert run.gate_correction is None

    # Re-opening is a no-op (migration is idempotent).
    disp2 = Dispatcher(db_path)
    assert disp2.get_dispatch("dsp-legacy").intent is DispatchIntent.IMPLEMENTATION


def test_resume_clears_correction_state(tmp_ws: Workspace):
    """Explicit resume restores a fresh correction budget rather than immediately
    re-tripping the previous guard."""
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL))
    gate = PublicBusGate(_bus(c), task_number=TASK)
    limits = DispatcherLimits(max_gate_corrections=2)
    fake = FakeExecutor(lead_script=[
        dispatch_to("worker", task_number=TASK),
        dispatch_to("worker", task_number=TASK),
    ])
    orch, disp = _orch(tmp_ws, fake, gate, limits=limits)
    orch.run(once=False)
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.WAITING

    resumed = disp.resume_run(run.id)
    assert resumed.consecutive_corrections == 0
    assert resumed.gate_correction is None
    assert RunStatus(resumed.status) is RunStatus.ACTIVE


# ── validation-order: a correction is recorded ONLY for a clean, valid
#    solicitation. A failed/malformed/stale/mis-acked solicitation combined with
#    a correctable public rejection must take the invalid/stale path and never be
#    downgraded to a benign correction (dispatcher kernel invariant, #64). ──
from crewd.session_backend import AttemptOutcome  # noqa: E402
from crewd.dispatcher import LEAD_PENDING  # noqa: E402

_ROLES = ("lead", "advisory", "worker", "verifier")
_CORRECTION = "public record needs repair"


def _open_run_and_solicit(tmp_ws: Workspace):
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    run = disp.start_or_resume_run(GOAL)
    sol = disp.open_lead_solicitation(run.id)
    disp.mark_started(sol.attempt_id, session_id="lead-sess", generation=0)
    return disp, run, sol


def _assert_not_corrected(disp, run_id, res):
    """A correction was NOT recorded: the run took the invalid/stale path."""
    assert res.gate_corrected is False
    assert res.solicitation_invalid is True
    run = disp.get_run(run_id)
    assert run.consecutive_corrections == 0
    assert run.gate_correction is None
    assert run.routing_authority == LEAD_PENDING


def test_correction_rejected_on_failed_outcome(tmp_ws: Workspace):
    """A correctable public rejection on a non-clean Lead turn (SDK error) is not
    a correction — it is an invalid/failed solicitation."""
    disp, run, sol = _open_run_and_solicit(tmp_ws)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.SDK_ERROR,
        decision=LeadDecision.dispatch("worker", ack=(), task_number=TASK),
        configured_roles=_ROLES,
        gate_correction=_CORRECTION,
    )
    _assert_not_corrected(disp, run.id, res)


def test_correction_rejected_on_missing_decision(tmp_ws: Workspace):
    """A correctable rejection with no captured decision (malformed turn) is not a
    correction."""
    disp, run, sol = _open_run_and_solicit(tmp_ws)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=None,
        configured_roles=_ROLES,
        gate_correction=_CORRECTION,
    )
    _assert_not_corrected(disp, run.id, res)


def test_correction_rejected_on_stale_authority(tmp_ws: Workspace):
    """A correctable rejection under a stale authority window (the nonce advanced
    since the solicitation opened) is not a correction — the candidate is stale
    and must be dropped, never applied as a correction under a later window."""
    disp, run, sol = _open_run_and_solicit(tmp_ws)
    # Advance the authority nonce out from under the open solicitation.
    with disp._txn() as c:  # noqa: SLF001 — deliberate white-box stale-window setup
        c.execute(
            "UPDATE goal_run SET authority_seq = authority_seq + 1 WHERE id = ?",
            (run.id,),
        )
    res = disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.dispatch("worker", ack=(), task_number=TASK),
        configured_roles=_ROLES,
        gate_correction=_CORRECTION,
    )
    _assert_not_corrected(disp, run.id, res)


def test_correction_rejected_on_wrong_acks(tmp_ws: Workspace):
    """A correctable rejection whose decision acknowledges the wrong pending-handoff
    set is not a correction."""
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    run = disp.start_or_resume_run(GOAL)
    # Seed exactly one pending handoff so the snapshot is non-empty.
    d = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=_ROLES)
    aid = disp.reserve_attempt(run.id, d.dispatch.id, "worker")
    disp.record_terminal(aid, AttemptOutcome.IDLE_COMPLETED)
    sol = disp.open_lead_solicitation(run.id)
    disp.mark_started(sol.attempt_id, session_id="lead-sess", generation=0)
    # Ack the wrong (empty) set vs the snapshot's pending handoff.
    res = disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.dispatch("worker", ack=(), task_number=TASK),
        configured_roles=_ROLES,
        gate_correction=_CORRECTION,
    )
    _assert_not_corrected(disp, run.id, res)


def test_correction_rejected_on_unconfigured_role(tmp_ws: Workspace):
    """A correctable rejection dispatching to an unconfigured role is not a
    correction."""
    disp, run, sol = _open_run_and_solicit(tmp_ws)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.dispatch("ghost", ack=(), task_number=TASK),
        configured_roles=_ROLES,
        gate_correction=_CORRECTION,
    )
    _assert_not_corrected(disp, run.id, res)


def test_correction_recorded_on_clean_valid_solicitation(tmp_ws: Workspace):
    """The positive control: a clean, valid solicitation whose only defect is the
    public record DOES record a typed, non-mutating correction and returns
    authority to Lead."""
    disp, run, sol = _open_run_and_solicit(tmp_ws)
    res = disp.resolve_lead_solicitation(
        sol.attempt_id,
        outcome=AttemptOutcome.IDLE_COMPLETED,
        decision=LeadDecision.dispatch("worker", ack=(), task_number=TASK),
        configured_roles=_ROLES,
        gate_correction=_CORRECTION,
    )
    assert res.gate_corrected is True
    assert res.solicitation_invalid is False
    got = disp.get_run(run.id)
    assert got.consecutive_corrections == 1
    assert got.gate_correction == _CORRECTION
    assert got.routing_authority == LEAD_PENDING
    assert RunStatus(got.status) is RunStatus.ACTIVE
