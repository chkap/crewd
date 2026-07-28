"""Orchestrator integration for durable public writes + consume enforcement (#29).

Proves the orchestrator, wired with a :class:`crewd.public_writer.PublicWriter`
over a deterministic fake GitHub boundary:

* **Pre-application Lead-dispatch gate** — a Lead dispatch decision's public
  artifact is reserved/posted/verified BEFORE ``resolve_lead_solicitation`` applies
  it; on an outage the candidate decision is dropped, so routing authority stays
  ``lead_pending``, no role dispatch is created/consumed, and a reserved intent is
  reconciled idempotently on a later run (no duplicate) before the decision applies
  (GOAL.md: material Lead routing is public before internal progress; an unverified
  write is never proof).
* publishes a material role handoff as a verified attributed artifact at the role's
  terminal, keyed by the durable handoff id;
* refuses to *consume* (ack) a material handoff whose public artifact is not yet
  verified — the handoff stays pending and the run pauses;
* treats a genuinely empty ``no_progress`` handoff as non-material (no artifact),
  but a ``no_progress`` with changed/remaining state as material.

No SDK, no network.
"""
from __future__ import annotations

from crewd.config import CrewConfig, GoalState
from crewd.dispatcher import Dispatcher, LEAD_PENDING, RunStatus
from crewd.executor import RoleHandoff
from crewd.github_bus import GitHubError, GitHubErrorKind, PublicBus
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


def _lead_dispatch_posts(c: FakeGitHubClient):
    return [p for p in _crew_posts(c) if "Lead decision" in p.body]


# ── happy path ──────────────────────────────────────────────────────────
def test_lead_dispatch_and_handoff_published_and_verified(tmp_ws: Workspace):
    """Valid record + healthy bus: the Lead dispatch artifact is published before
    the decision applies, the Worker runs, its material handoff is published +
    verified, and the wait consumes it."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    pub = _writer(tmp_ws, c)
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_material_handoff,
    )
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    assert [r.role for r in fake.role_calls] == ["worker"]
    # Both artifacts present + verified: the Lead dispatch decision and the worker
    # readiness handoff (worker -> verifier).
    assert len(_lead_dispatch_posts(c)) == 1
    assert any("worker \u2192 verifier" in p.body for p in _crew_posts(c))
    assert pub.counts()["pending"] == 0 and pub.counts()["verified"] >= 2
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"


# ── pre-application Lead-dispatch gate (the Verifier repro) ──────────────
def test_lead_dispatch_blocked_when_write_unverified(tmp_ws: Workspace):
    """A sustained write outage means the Lead dispatch decision cannot be
    published; the decision must NOT be applied — routing authority stays
    ``lead_pending``, no role dispatch is created/consumed, and the reserved
    intent is left for reconciliation."""
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

    # No role ever ran and no dispatch artifact verified.
    assert fake.role_calls == []
    assert _crew_posts(c) == []
    run = disp.start_or_resume_run(GOAL)
    # Authority stayed with Lead; the run paused with the descriptive blocker.
    assert run.routing_authority == LEAD_PENDING
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert "lead dispatch decision unverified" in (run.human_blocker or "")
    # A durable reserved intent exists for the Lead dispatch, unverified.
    pending = pub.store.list_pending()
    assert len(pending) == 1
    assert pending[0].role == "lead" and pending[0].state == WriteState.RESERVED.value
    assert pub.counts()["verified"] == 0
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


def test_lead_dispatch_reconciled_without_duplicate_then_applied(tmp_ws: Workspace):
    """Once the outage clears, the reserved Lead-dispatch intent reconciles on the
    next run (exactly one post, no duplicate) and the decision then applies."""
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
    assert pub.counts()["verified"] == 0  # blocked/paused above
    assert c.create_calls >= 1            # at least one failed write was attempted
    disp.close()

    # Outage clears; a fresh run over the same durable state reconciles the reserved
    # Lead-dispatch intent on start (idempotent marker → no duplicate), then Lead
    # re-decides dispatch(worker) with the SAME stable correlation id → deduped →
    # verified → the decision applies and the Worker runs.
    c.persistent_faults.clear()
    creates_before = c.create_calls
    pub2 = _writer(tmp_ws, c)
    fake2 = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_material_handoff,
    )
    disp2 = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    orch2, _ = _orch(tmp_ws, fake2, pub2, disp=disp2)
    orch2.run(once=False, resume=True)

    # Exactly one Lead-dispatch artifact exists (reconcile + re-decide did not
    # double-post), the worker ran, and its handoff is verified too.
    assert len(_lead_dispatch_posts(c)) == 1
    assert [r.role for r in fake2.role_calls] == ["worker"]
    assert pub2.counts()["pending"] == 0
    run = disp2.start_or_resume_run(GOAL)
    assert [h.role for h in disp2.pending_handoffs(run.id)] == []


# ── role-handoff consume gate (outage begins AFTER the dispatch write) ───
def test_consume_blocked_when_handoff_write_unverified(tmp_ws: Workspace):
    """The Lead dispatch artifact publishes (call 1), then the bus goes down so the
    Worker's material handoff cannot verify; Lead may not consume it — the run
    pauses and the worker handoff stays pending."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    # First create (the Lead dispatch artifact) succeeds; every later write fails.
    c.create_fail_after = 1
    c.create_fail_error = GitHubError(GitHubErrorKind.TIMEOUT, "outage")
    pub = _writer(tmp_ws, c)
    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_material_handoff,
    )
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    # The dispatch published, so the Worker DID run; its handoff write never verified.
    assert [r.role for r in fake.role_calls] == ["worker"]
    assert len(_lead_dispatch_posts(c)) == 1
    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert "public-bus write unverified" in (run.human_blocker or "")
    pending = disp.pending_handoffs(run.id)
    assert [h.role for h in pending] == ["worker"]
    worker_intent = pub.store.get(pending[0].id)
    assert worker_intent is not None and worker_intent.state == WriteState.RESERVED.value
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


# ── materiality: bare no_progress private; changed/remaining material ────
def test_bare_no_progress_needs_no_artifact(tmp_ws: Workspace):
    """A genuinely empty ``no_progress`` is non-material: the dispatch publishes,
    the Worker returns bare no-progress (no reserved artifact), and Lead consumes
    it even though later writes would fail."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    c.create_fail_after = 1  # only the Lead dispatch write may succeed
    pub = _writer(tmp_ws, c)

    def _bare(role, req):
        return RoleHandoff(outcome_class="no_progress", reason="nothing to do yet")

    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_bare,
    )
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    assert [r.role for r in fake.role_calls] == ["worker"]
    # No worker artifact was ever reserved (non-material), and the wait consumed it.
    assert [i for i in pub.store.list_pending() if i.role == "worker"] == []
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"
    run = disp.start_or_resume_run(GOAL)
    assert [h.role for h in disp.pending_handoffs(run.id)] == []


def test_material_no_progress_requires_artifact(tmp_ws: Workspace):
    """A ``no_progress`` that reports changed/remaining state IS material: with the
    bus down after the dispatch write, its consume is blocked and it stays
    pending — a material no-progress is not hidden as private."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    c.create_fail_after = 1  # dispatch write ok; the handoff write fails
    pub = _writer(tmp_ws, c)

    def _material_np(role, req):
        return RoleHandoff(outcome_class="no_progress",
                           reason="blocked mid-way",
                           changed="left a partial migration on disk",
                           remaining="finish the migration")

    fake = FakeExecutor(
        lead_script=[dispatch_to("worker"), wait("external")],
        role_handoff=_material_np,
    )
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert "public-bus write unverified" in (run.human_blocker or "")
    pending = disp.pending_handoffs(run.id)
    assert [h.role for h in pending] == ["worker"]
    # A durable worker intent was reserved (materiality forced a required artifact).
    assert pub.store.get(pending[0].id) is not None
