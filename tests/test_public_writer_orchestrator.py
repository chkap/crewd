"""Orchestrator integration for durable public writes + consume enforcement (#29).

Proves the orchestrator, wired with a :class:`crewd.public_writer.PublicWriter`
over a deterministic fake GitHub boundary:

* publishes a material role handoff as a verified attributed artifact at the
  role's terminal, keyed by the durable handoff id;
* refuses to *consume* (ack) a material handoff whose public artifact is not yet
  verified — the handoff stays pending and the run pauses, so a crashed/unavailable
  public write never becomes a silently-completed handoff (GOAL #2/#5);
* reconciles a reserved-but-unverified intent on the next run once GitHub recovers;
* treats a private ``no_progress`` handoff as non-material (no artifact required).

No SDK, no network.
"""
from __future__ import annotations

from crewd.config import CrewConfig, GoalState
from crewd.dispatcher import Dispatcher, RunStatus
from crewd.executor import RoleHandoff
from crewd.github_bus import GitHubErrorKind, PublicBus
from crewd.orchestrator import Orchestrator
from crewd.public_writer import IntentStore, PublicWriter, WriteState
from crewd.workspace import Workspace

from fakes import FakeExecutor, dispatch_to, wait
from fake_github import FakeGitHubClient

CREW = "testcrew"
REPO = "acme/widget"
GOAL = "goal:v2"
TASK = 29


def _valid_worker_record(c: FakeGitHubClient) -> None:
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", labels=("crewd:task", GOAL), assignees=("alice",))
    c.add_comment("issue", TASK, f"> **[crewd:lead -> worker]** {CREW}\n\nAssigned.")


def _writer(tmp_ws: Workspace, c: FakeGitHubClient) -> PublicWriter:
    bus = PublicBus(c, crew=CREW, expected_repo=REPO, goal_label=GOAL)
    return PublicWriter(bus, IntentStore.for_workspace(tmp_ws))


def _orch(tmp_ws: Workspace, fake: FakeExecutor, publisher, disp=None) -> tuple[Orchestrator, Dispatcher]:
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = 0
    gs = GoalState(version=1, label=GOAL, cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    disp = disp or Dispatcher(tmp_ws.state_dir / "dispatch.db")
    orch = Orchestrator(tmp_ws, cfg, fake, gs, dispatcher=disp,
                        publisher=publisher, max_steps=50)
    return orch, disp


def _material_handoff(role: str, req):
    return RoleHandoff(outcome_class="completed", evidence="PR #99 green",
                       changed="added module")


def _crew_posts(c: FakeGitHubClient):
    return [x for x in c.list_comments(target="issue", number=TASK)
            if "crewd:correlation:" in x.body]


def test_material_handoff_published_at_terminal(tmp_ws: Workspace):
    """A dispatched Worker's material handoff is published + verified at terminal."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    pub = _writer(tmp_ws, c)
    # Lead dispatches worker, then waits (acking the worker handoff once verified).
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_material_handoff,
    )
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    assert [r.role for r in fake.role_calls] == ["worker"]
    posts = _crew_posts(c)
    # worker->verifier readiness present and verified in the durable journal.
    assert any("worker \u2192 verifier" in p.body for p in posts)
    assert pub.counts()["pending"] == 0 and pub.counts()["verified"] >= 1
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"


def test_consume_blocked_when_public_write_unverified(tmp_ws: Workspace):
    """A sustained GitHub outage leaves the Worker handoff unpublished; Lead may
    not consume it — the run pauses and the handoff stays pending (no ack)."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    # Sustained outage on writes only (reads still validate the dispatch prereq).
    c.always_fail("create_comment", GitHubErrorKind.TIMEOUT, "outage")
    pub = _writer(tmp_ws, c)
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_material_handoff,
    )
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    # Worker ran and produced a handoff, but the write never verified.
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert _crew_posts(c) == []
    # The consume was refused: run paused with a public-bus write blocker, and the
    # worker handoff remains pending (unconsumed) with a reserved durable intent.
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert "public-bus write unverified" in (run.human_blocker or "")
    pending = disp.pending_handoffs(run.id)
    assert [h.role for h in pending] == ["worker"]
    # The worker handoff's own durable intent is reserved (unverified), never
    # silently marked done.
    intent = pub.store.get(pending[0].id)
    assert intent is not None and intent.state == WriteState.RESERVED.value
    assert pub.counts()["verified"] == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


def test_reconcile_on_restart_then_consume_allowed(tmp_ws: Workspace):
    """After the outage clears, the next run reconciles the reserved intent and
    Lead can then consume the handoff."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    c.always_fail("create_comment", GitHubErrorKind.TIMEOUT, "outage")
    pub = _writer(tmp_ws, c)
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_material_handoff,
    )
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)
    assert pub.counts()["verified"] == 0  # blocked/paused, nothing verified
    disp.close()

    # Outage clears; a fresh run over the same durable state reconciles the
    # reserved intent on start, then Lead's retried wait consumes the handoff.
    c.persistent_faults.clear()
    pub2 = _writer(tmp_ws, c)
    fake2 = FakeExecutor(lead_script=[wait("external")])
    # Resume the paused run explicitly (mirrors the operator `resume` workflow).
    disp2 = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    orch2, _ = _orch(tmp_ws, fake2, pub2, disp=disp2)
    orch2.run(once=False, resume=True)

    assert pub2.counts()["pending"] == 0 and pub2.counts()["verified"] >= 1
    assert len(_crew_posts(c)) >= 1
    run = disp2.start_or_resume_run(GOAL)
    # The worker handoff is now consumed (no longer pending).
    assert [h.role for h in disp2.pending_handoffs(run.id)] == []


def test_private_no_progress_not_published(tmp_ws: Workspace):
    """A bare private ``no_progress`` handoff is not material: no artifact is
    required and Lead may consume it even with the bus otherwise unavailable."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    c.always_fail("create_comment", GitHubErrorKind.TIMEOUT, "outage")
    pub = _writer(tmp_ws, c)

    def _bare(role, req):
        return RoleHandoff(outcome_class="no_progress", reason="nothing to do yet")

    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_bare,
    )
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    # The worker's bare no_progress required no artifact: no worker-role intent was
    # ever reserved, and the wait consumed the handoff despite the write outage.
    worker_intents = [i for i in pub.store.list_pending() if i.role == "worker"]
    assert worker_intents == []
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"
    run = disp.start_or_resume_run(GOAL)
    assert [h.role for h in disp.pending_handoffs(run.id)] == []
