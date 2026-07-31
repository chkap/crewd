"""Host-side durable evidence discovery for lost role handoffs (issue #65).

The live #64 incident chain ends with a Worker attempt that reaches a clean idle
having done real durable work (a pushed branch, an opened/mergeable PR, green
checks) but double-calls ``submit_role_handoff``. Production capture semantics
(``crewd.executor._SingleSubmitCapture``) make a duplicate submission return NO
payload, so the orchestrator sees ``handoff=None`` and the role's own evidence is
irrecoverable. These tests prove the host recovers that evidence directly from
the exact bound task's GitHub record so Lead routes on durable facts — never a
fabricated readiness — preserving the exact task/PR binding for an exact-bound
Verifier review, and never blindly repeating Worker or fabricating a completion.

Coverage:
* real capture semantics: a duplicate submission loses the payload;
* ``EvidenceDiscovery`` unit behaviour: green/merged/none/exact-bound/transient;
* ``resolve_role_terminal`` + discovery: uncertain-with-empty-evidence enriched
  from GitHub, class unchanged;
* end-to-end incident chain through the real orchestrator/executor capture path
  (two SDK aborts → duplicate-submit idle) with a fake GitHub record: evidence
  recovered, PR bound to the exact dispatch, run WAITs, no false pause, Worker
  not blindly repeated;
* restart durability of the recovered PR binding;
* adversarial: no linked PR (no fabricated evidence), an unrelated PR that merely
  mentions the issue is not mis-bound, and discovery never upgrades to completed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from crewd.config import CrewConfig, GoalState
from crewd.dispatcher import Dispatcher, RunStatus
from crewd.executor import (
    AttemptResult,
    RoleHandoff,
    RoleHandoffCapture,
    resolve_role_terminal,
)
from crewd.github_bus import (
    EvidenceDiscovery,
    GitHubError,
    GitHubErrorKind,
    RecoveredEvidence,
    summarize_check_rollup,
)
from crewd.orchestrator import Orchestrator
from crewd.session_backend import AttemptOutcome
from crewd.workspace import Workspace

from fake_github import FakeGitHubClient
from fakes import FakeExecutor, dispatch_to


# ── real capture semantics: a duplicate submission loses the payload ──
def test_duplicate_submission_loses_structured_payload():
    """The production exactly-one capture returns None on a double submit, so no
    structured evidence survives — this is why host-side recovery is required."""
    cap = RoleHandoffCapture()
    first = cap.submit(RoleHandoff("completed", evidence="PR #99", changed="opened PR"))
    second = cap.submit(RoleHandoff("completed", evidence="PR #99", changed="opened PR"))
    assert first is True
    assert second is False
    assert cap.count == 2
    assert cap.result() is None  # payload is unrecoverable from the capture


def _idle_result() -> AttemptResult:
    return AttemptResult(
        attempt_id="att-1",
        session_id="sess-worker",
        role="worker",
        outcome=AttemptOutcome.IDLE_COMPLETED,
        duration=0.0,
    )


def test_resolve_role_terminal_uncertain_on_lost_handoff():
    """A clean idle with a lost handoff (None, submissions=2) is uncertain with no
    structured evidence — the precondition host discovery must repair."""
    term = resolve_role_terminal(_idle_result(), None, 2)
    assert term.outcome_class.value == "uncertain"
    assert term.evidence == ""
    assert "protocol_failure" in term.reason_returned


# ── EvidenceDiscovery unit behaviour ──
def _client_with_pr(**kw) -> FakeGitHubClient:
    c = FakeGitHubClient("acme/widget")
    c.add_pull(99, "impl #65", **kw)
    return c


def test_discovery_recovers_open_green_pr():
    c = _client_with_pr(
        state="open", body="Closes #65", linked_issues=(65,),
        head_ref="crewd/issue-65", mergeable="MERGEABLE", checks="passing",
    )
    rec = EvidenceDiscovery(c).discover(task_number=65)
    assert rec.found
    assert rec.pr_number == 99
    assert rec.pr_state == "open"
    assert rec.branch == "crewd/issue-65"
    assert rec.mergeable == "MERGEABLE"
    assert rec.checks == "passing"
    assert "PR #99" in rec.render() and "branch crewd/issue-65" in rec.render()


def test_discovery_ambiguous_multiple_pulls_fails_closed():
    """When several PRs mention the task and none is authoritatively bound,
    discovery fails closed (found=False) rather than guessing a binding."""
    c = FakeGitHubClient("acme/widget")
    c.add_pull(70, "candidate A", state="open", body="#65", linked_issues=(65,))
    c.add_pull(71, "candidate B", state="merged", body="#65", linked_issues=(65,))
    rec = EvidenceDiscovery(c).discover(task_number=65)
    assert rec is not None and not rec.found


def test_discovery_ambiguous_resolves_when_pr_bound():
    """A previously bound PR disambiguates: only that exact PR is accepted even
    when other PRs also mention the task."""
    c = FakeGitHubClient("acme/widget")
    c.add_pull(70, "candidate A", state="open", body="#65", linked_issues=(65,),
               head_ref="crewd/issue-65", checks="passing")
    c.add_pull(71, "candidate B", state="merged", body="#65", linked_issues=(65,))
    rec = EvidenceDiscovery(c).discover(task_number=65, pr_number=70)
    assert rec.found and rec.pr_number == 70 and rec.branch == "crewd/issue-65"


def test_discovery_none_when_no_linked_pr():
    c = FakeGitHubClient("acme/widget")
    c.add_pull(99, "unrelated", state="open", body="no link", linked_issues=())
    rec = EvidenceDiscovery(c).discover(task_number=65)
    assert rec is not None
    assert not rec.found
    assert "no linked PR/branch" in rec.render()


def test_discovery_exact_pr_binding_ignores_unrelated_pr():
    """When a specific PR was already bound, only that exact PR is accepted even if
    another open PR also mentions the issue (no mis-binding to a stale PR)."""
    c = FakeGitHubClient("acme/widget")
    c.add_pull(99, "the bound PR", state="open", body="Closes #65",
               linked_issues=(65,), head_ref="crewd/issue-65")
    c.add_pull(123, "unrelated mention", state="open", body="see #65",
               linked_issues=(65,))
    rec = EvidenceDiscovery(c).discover(task_number=65, pr_number=99)
    assert rec.pr_number == 99


def test_discovery_transient_error_returns_none():
    c = _client_with_pr(state="open", body="#65", linked_issues=(65,))
    c.always_fail("list_pulls", GitHubErrorKind.TIMEOUT, "outage")
    assert EvidenceDiscovery(c).discover(task_number=65) is None


def test_discovery_tolerates_get_pull_outage_with_list_fallback():
    """If the per-PR re-read fails transiently, discovery still returns the facts
    already known from the list view rather than discarding the recovery."""
    c = _client_with_pr(state="open", body="#65", linked_issues=(65,),
                        head_ref="crewd/issue-65", checks="passing")
    c.always_fail("get_pull", GitHubErrorKind.TIMEOUT, "outage")
    rec = EvidenceDiscovery(c).discover(task_number=65)
    assert rec.found and rec.pr_number == 99 and rec.branch == "crewd/issue-65"


def test_summarize_check_rollup_states():
    assert summarize_check_rollup([{"conclusion": "SUCCESS"}]) == "passing"
    assert summarize_check_rollup([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"
    assert summarize_check_rollup([{"status": "IN_PROGRESS"}]) == "pending"
    # A bare COMPLETED status with no conclusion must NOT optimistically pass.
    assert summarize_check_rollup([{"status": "COMPLETED"}]) == "pending"
    assert summarize_check_rollup([]) == "none"
    assert summarize_check_rollup(None) == "unknown"
    assert summarize_check_rollup("weird") == "unknown"


# ── end-to-end incident chain through the real orchestrator/executor path ──
def _orch(tmp_ws: Workspace, fake: FakeExecutor, discovery, *, max_steps=100):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = 0
    gs = GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    orch = Orchestrator(
        tmp_ws, cfg, fake, gs, dispatcher=disp,
        evidence_discovery=discovery, max_steps=max_steps,
    )
    return orch, disp


def _incident_fake() -> FakeExecutor:
    # Two clean Worker SDK aborts, then every later Worker tick reaches a clean
    # idle but double-submits its handoff (payload lost — no injected RoleHandoff).
    outcomes = iter([AttemptOutcome.ABORTED_CLEAN, AttemptOutcome.ABORTED_CLEAN])

    def role_outcome(req):
        return next(outcomes, AttemptOutcome.IDLE_COMPLETED)

    return FakeExecutor(
        lead_script=[dispatch_to("worker", task_number=65)] * 30,
        role_outcome=role_outcome,
        role_handoff=None,           # duplicate submission → capture yields None
        role_handoff_submissions=2,
    )


def test_incident_chain_recovers_evidence_binds_pr_waits_no_false_pause(tmp_ws: Workspace):
    client = FakeGitHubClient("acme/widget")
    client.add_pull(
        99, "implement #65", state="open", body="Closes #65", linked_issues=(65,),
        head_ref="crewd/issue-65-bounded", mergeable="MERGEABLE", checks="passing",
    )
    fake = _incident_fake()
    orch, disp = _orch(tmp_ws, fake, EvidenceDiscovery(client))
    assert orch.run(once=False) == 0

    # Bounded WAIT, never a fabricated human pause.
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"
    run = disp.start_or_resume_run("goal:v1")
    assert RunStatus(run.status) is RunStatus.WAITING
    assert run.human_blocker is None

    worker_handoffs = [
        h for h in disp.export_run(run.id)["handoffs"] if h["role"] == "worker"
    ]
    assert worker_handoffs
    # No fabricated completion: the lost-handoff ticks stay uncertain...
    idle_uncertain = [
        h for h in worker_handoffs
        if h["outcome_class"] == "uncertain" and "host-recovered" in (h["reason_returned"] or "")
    ]
    assert idle_uncertain
    assert all(h["outcome_class"] != "completed" for h in worker_handoffs)
    # ...but each carries the host-recovered durable branch/PR/check evidence.
    for h in idle_uncertain:
        ev = h["evidence"] or ""
        assert "host-recovered durable evidence" in ev
        assert "PR #99" in ev
        assert "crewd/issue-65-bounded" in ev
        assert "checks=passing" in ev

    # Exact task/PR binding preserved: the discovered PR is bound to the dispatch
    # so a later Verifier reviews that exact PR.
    hid = idle_uncertain[0]["id"]
    task_number, pr_number = disp.binding_for_handoff(hid)
    assert task_number == 65
    assert pr_number == 99


def test_incident_chain_recovered_binding_survives_restart(tmp_ws: Workspace):
    """The recovered PR binding is durable: reopening the dispatcher on the same db
    still reports the exact #65/PR #99 binding (no replay, no lost evidence)."""
    client = FakeGitHubClient("acme/widget")
    client.add_pull(99, "impl #65", state="open", body="Closes #65",
                    linked_issues=(65,), head_ref="crewd/issue-65", checks="passing")
    fake = _incident_fake()
    orch, disp = _orch(tmp_ws, fake, EvidenceDiscovery(client))
    orch.run(once=False)
    run = disp.start_or_resume_run("goal:v1")
    hid = next(
        h["id"] for h in disp.export_run(run.id)["handoffs"]
        if h["role"] == "worker" and "host-recovered" in (h["reason_returned"] or "")
    )
    disp.close()

    reopened = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    try:
        assert reopened.binding_for_handoff(hid) == (65, 99)
    finally:
        reopened.close()


def test_incident_chain_no_linked_pr_yields_no_fabricated_evidence(tmp_ws: Workspace):
    """Adversarial: the role botched its handoff AND produced no durable PR. The
    host must NOT fabricate evidence — the tick stays uncertain with none of the
    host-recovered annotation, and the run still WAITs (no false pause)."""
    client = FakeGitHubClient("acme/widget")  # no PR linking task #65
    fake = _incident_fake()
    orch, disp = _orch(tmp_ws, fake, EvidenceDiscovery(client))
    assert orch.run(once=False) == 0

    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"
    run = disp.start_or_resume_run("goal:v1")
    assert RunStatus(run.status) is RunStatus.WAITING
    assert run.human_blocker is None
    worker_handoffs = [
        h for h in disp.export_run(run.id)["handoffs"] if h["role"] == "worker"
    ]
    assert worker_handoffs
    assert all("host-recovered" not in (h["reason_returned"] or "") for h in worker_handoffs)
    assert all(not (h["evidence"] or "").strip() for h in worker_handoffs
               if h["outcome_class"] == "uncertain")


def test_discovery_never_upgrades_class_to_completed(tmp_ws: Workspace):
    """Even with a green mergeable PR, a botched handoff can never be upgraded to a
    completion — the recovered evidence only informs Lead's routing."""
    client = FakeGitHubClient("acme/widget")
    client.add_pull(99, "impl #65", state="open", body="Closes #65",
                    linked_issues=(65,), head_ref="crewd/issue-65",
                    mergeable="MERGEABLE", checks="passing")
    fake = _incident_fake()
    orch, disp = _orch(tmp_ws, fake, EvidenceDiscovery(client))
    orch.run(once=False)
    run = disp.start_or_resume_run("goal:v1")
    worker_handoffs = [
        h for h in disp.export_run(run.id)["handoffs"] if h["role"] == "worker"
    ]
    assert worker_handoffs
    assert all(h["outcome_class"] != "completed" for h in worker_handoffs)


# ── the recovered/bound PR flows through the production Verifier gate ──
def _verifier_ready_client() -> FakeGitHubClient:
    """A public record where task #65 is dispatchable to Verifier but TWO open
    PRs link it (ambiguous) — only an authoritative bound PR can disambiguate."""
    c = FakeGitHubClient("acme/widget")
    c.add_issue(100, "GOAL: bounded recovery", labels=("goal:v1",))
    c.add_issue(65, "task: bounded recovery", labels=("crewd:task", "goal:v1"),
                assignees=("alice",))
    c.add_pull(98, "stray/duplicate", state="open", body="also #65", linked_issues=(65,))
    c.add_pull(99, "the real impl", state="open", body="Closes #65", linked_issues=(65,),
               head_ref="crewd/issue-65", mergeable="MERGEABLE", checks="passing")
    c.add_comment("issue", 65, "> **[crewd:worker -> verifier]** testcrew\n\nReady: PR #99.")
    return c


def _gate(client):
    from crewd.github_bus import PublicBus, PublicBusGate

    bus = PublicBus(client, crew="testcrew", expected_repo="acme/widget",
                    goal_label="goal:v1")
    return PublicBusGate(bus)


def _orch_with_gate(tmp_ws, gate):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = 0
    gs = GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    disp = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    orch = Orchestrator(tmp_ws, cfg, FakeExecutor(lead_script=[]), gs,
                        dispatcher=disp, bus_gate=gate, max_steps=50)
    return orch, disp


def _bind_worker_pr(disp, run, pr):
    """Simulate the host having recovered+bound PR ``pr`` on a prior Worker
    dispatch for task #65 (the #64 lost-handoff chain outcome)."""
    from crewd.dispatcher import LeadDecision
    res = disp.lead_decide(
        run.id, LeadDecision.dispatch("worker", task_number=65),
        configured_roles=("lead", "advisory", "worker", "verifier"),
    )
    disp.bind_pr_to_dispatch(res.dispatch.id, pr)
    att = disp.reserve_attempt(run.id, res.dispatch.id, "worker")
    disp.mark_started(att, session_id="s", generation=0)
    disp.record_terminal(att, AttemptOutcome.IDLE_COMPLETED, evidence="recovered")


def test_pre_application_verifier_gate_inherits_bound_pr_over_ambiguity(tmp_ws: Workspace):
    """The Lead-time (pre-application) Verifier gate inherits the exact PR bound to
    the task by the prior Worker recovery, so it proceeds against that PR instead
    of failing closed on the two ambiguous linked PRs (#65 Verifier-routing fix)."""
    client = _verifier_ready_client()
    orch, disp = _orch_with_gate(tmp_ws, _gate(client))
    run = disp.start_or_resume_run("goal:v1")
    _bind_worker_pr(disp, run, 99)

    # With the inherited binding, the pre-application verifier gate proceeds.
    block = orch._dispatch_gate_block("verifier", task_number=65, intent="implementation")
    assert block is None


def test_pre_application_verifier_gate_ambiguous_without_binding(tmp_ws: Workspace):
    """Control: with no bound PR, the same two ambiguous linked PRs make the
    pre-application Verifier gate fail closed rather than guess."""
    client = _verifier_ready_client()
    orch, disp = _orch_with_gate(tmp_ws, _gate(client))
    disp.start_or_resume_run("goal:v1")  # no worker PR binding

    block = orch._dispatch_gate_block("verifier", task_number=65, intent="implementation")
    assert block is not None
    # Precisely the multiple-linked-PR ambiguity, not an earlier prereq failure.
    assert "multiple open PRs" in block.detail or "multiple" in block.detail.lower()
