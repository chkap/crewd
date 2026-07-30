"""Self-healing terminal public-write recovery regression matrix (issue #49).

Proves that a terminal public write racing a PR-merge auto-close — or hitting a
transient GitHub failure — self-heals (WAIT → reconcile on the next run) instead
of being mis-reported as a human blocker (``PAUSED`` / ``human-blocked``), while a
*genuine* permission/credential/policy denial still pauses for an operator.

Covered:
* ``classify_gh_stderr`` maps a closed/locked/archived/read-only target to a
  recoverable (non-permission) kind, and reserves ``PERMISSION`` for a real
  credential/policy denial.
* A terminal handoff published to a *closed* task (a merged PR auto-closed it)
  still posts + verifies — GitHub permits comments on a closed issue, so a
  verified linked merge does not block the terminal record; and a reconcile of
  that closed target is idempotent (no duplicate).
* WAIT-vs-PAUSE orchestrator matrix: a sustained transient outage WAITS and
  self-heals; a sustained permission denial PAUSES as a genuine human blocker.
* End-to-end self-heal: a transient outage leaves the run WAITING; once cleared a
  resume reconciles the terminal write (no duplicate) and the run continues to the
  next task with NO operator mutation of the record.
* Diagnostics distinguish a self-healing pending write from one that genuinely
  needs an operator.

No SDK, no network.
"""
from __future__ import annotations

from crewd.config import CrewConfig, GoalState
from crewd.dispatcher import Dispatcher, LEAD_PENDING, RunStatus
from crewd.diagnostics import build_snapshot
from crewd.executor import RoleHandoff
from crewd.github_bus import (
    GitHubErrorKind,
    PublicBus,
    Route,
    classify_gh_stderr,
)
from crewd.orchestrator import Orchestrator
from crewd.public_writer import IntentStore, PublicWriter, WriteState
from crewd.workspace import Workspace

from fakes import FakeExecutor, dispatch_to, wait
from fake_github import FakeGitHubClient

CREW = "testcrew"
REPO = "acme/widget"
GOAL = "goal:v2"
TASK = 29


# ── helpers (mirror test_public_writer_orchestrator) ────────────────────
def _valid_worker_record(c: FakeGitHubClient, *, task_state: str = "open",
                         merged_pr: int = 0) -> None:
    c.add_issue(30, "GOAL: x", labels=(GOAL,))
    c.add_issue(TASK, "task", state=task_state, labels=("crewd:task", GOAL),
                assignees=("alice",))
    c.add_comment("issue", TASK, f"> **[crewd:lead -> worker]** {CREW}\n\nAssigned.")
    # Model closure provenance: a closed task is legitimately closed only by a
    # merged PR that links it (issue #49 authorization). Tests that close the task
    # supply the merged linked PR that caused the auto-close.
    if merged_pr:
        c.add_pull(merged_pr, "impl", state="merged", linked_issues=(TASK,))


def _writer(tmp_ws: Workspace, c: FakeGitHubClient) -> PublicWriter:
    bus = PublicBus(c, crew=CREW, expected_repo=REPO, goal_label=GOAL)
    return PublicWriter(bus, IntentStore.for_workspace(tmp_ws))


def _orch(tmp_ws: Workspace, fake: FakeExecutor, publisher, disp=None):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = 0
    gs = GoalState(version=1, label=GOAL, cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    disp = disp or Dispatcher(tmp_ws.state_dir / "dispatch.db")
    orch = Orchestrator(tmp_ws, cfg, fake, gs, dispatcher=disp,
                        publisher=publisher, max_steps=50)
    return orch, disp


def _material_handoff(role: str, req):
    return RoleHandoff(outcome_class="completed", evidence="PR #99 merged (green)",
                       changed="added module")


def _crew_posts(c: FakeGitHubClient):
    return [x for x in c.list_comments(target="issue", number=TASK)
            if "crewd:correlation:" in x.body]


# ── 1. classification: closure race vs genuine permission ───────────────
def test_classify_closed_target_race_is_recoverable_not_permission():
    for stderr in (
        "GraphQL: Cannot add comments to a closed issue (addComment)",
        "unable to comment on a closed pull request",
    ):
        kind = classify_gh_stderr(stderr, 1)
        assert kind is not GitHubErrorKind.PERMISSION, stderr
        assert kind is GitHubErrorKind.TRANSIENT, stderr


def test_classify_persistent_locked_archived_surface_to_operator():
    # Locked / archived / read-only are persistent, operator-actionable states —
    # they must NOT be downgraded to a self-healing transient (issue #49 review).
    for stderr in (
        "403: Repository was archived so is read-only",
        "HTTP 403: this issue is locked",
    ):
        assert classify_gh_stderr(stderr, 1) is GitHubErrorKind.PERMISSION, stderr


def test_classify_genuine_permission_still_pauses():
    for stderr in (
        "HTTP 403: Resource not accessible by integration",
        "must have admin rights to Repository",
        "authentication required",
        "403 Forbidden",
    ):
        assert classify_gh_stderr(stderr, 1) is GitHubErrorKind.PERMISSION, stderr


def test_classify_transient_kinds_preserved():
    assert classify_gh_stderr("API rate limit exceeded", 1) is GitHubErrorKind.RATE_LIMIT
    assert classify_gh_stderr("request timed out", 1) is GitHubErrorKind.TIMEOUT
    assert classify_gh_stderr("404 not found", 1) is GitHubErrorKind.NOT_FOUND


# ── 2. terminal publish to a CLOSED task (merged PR auto-closed it) ──────
def test_terminal_handoff_publishes_to_closed_task(tmp_ws: Workspace):
    """A verified linked merge auto-closes the bound task before the terminal
    handoff is published; because the closure is provably caused by a merged
    linked PR the comment still posts + verifies (GitHub permits comments on a
    closed issue), so the terminal record is NOT human-blocked."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c, task_state="closed", merged_pr=99)
    pub = _writer(tmp_ws, c)

    outcome = pub.publish_role_handoff(
        handoff_id="ho-term", role="verifier", outcome_class="completed",
        task_number=TASK, pr_number=99, evidence="approved PR #99 (merged)", changed="none",
    )
    assert outcome.route is Route.PROCEED and outcome.verified
    assert pub.counts()["verified"] == 1 and pub.counts()["pending"] == 0


def test_terminal_handoff_to_unrelated_closed_task_is_invalid_target(tmp_ws: Workspace):
    """A task closed with NO merged linked PR is a stale/unrelated closure: the
    terminal write is rejected as an invalid target (typed), NOT posted and NOT
    parked as a self-healing WAIT — it will never legitimately land (issue #49)."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c, task_state="closed")  # no merged linked PR
    pub = _writer(tmp_ws, c)

    outcome = pub.publish_role_handoff(
        handoff_id="ho-term", role="verifier", outcome_class="completed",
        task_number=TASK, evidence="approved", changed="none",
    )
    assert outcome.route is Route.REJECT and not outcome.verified
    assert len(_crew_posts(c)) == 0
    pending = pub.store.list_pending()
    assert len(pending) == 1
    from crewd.public_writer import LifecyclePhase
    assert pending[0].phase == LifecyclePhase.INVALID_TARGET.value
    assert pending[0].disposition == "invalid_target"
    # An invalid target is never "due" for a self-healing retry.
    assert pub.store.list_due_pending() == []
    assert pub.has_operator_block()


def test_terminal_handoff_rejects_wrong_repository_goal_and_deleted_target(
    tmp_ws: Workspace,
):
    cases = []

    wrong_repo = FakeGitHubClient("evil/other")
    _valid_worker_record(wrong_repo)
    cases.append(wrong_repo)

    wrong_goal = FakeGitHubClient(REPO)
    wrong_goal.add_issue(TASK, "task", labels=("crewd:task", "goal:v1"))
    cases.append(wrong_goal)

    deleted = FakeGitHubClient(REPO)
    cases.append(deleted)

    for index, client in enumerate(cases):
        outcome = _writer(tmp_ws, client).publish_role_handoff(
            handoff_id=f"invalid-{index}", role="verifier",
            outcome_class="completed", task_number=TASK, changed="none",
        )
        assert outcome.route is Route.REJECT
        assert _crew_posts(client) == []


def test_terminal_closure_provenance_lookup_timeout_waits(tmp_ws: Workspace):
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c, task_state="closed", merged_pr=99)
    c.fail_once("list_pulls", GitHubErrorKind.TIMEOUT, "slow")

    outcome = _writer(tmp_ws, c).publish_role_handoff(
        handoff_id="lookup-timeout", role="verifier",
        outcome_class="completed", task_number=TASK, pr_number=99,
        changed="none",
    )
    assert outcome.route is Route.WAIT
    assert c.comments.get(("issue", TASK), [])[-1].body.endswith("Assigned.")


def test_closed_target_reconcile_is_idempotent(tmp_ws: Workspace):
    """Re-publishing / reconciling the same terminal write against a closed target
    dedupes on the correlation marker — exactly one comment, never a duplicate."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c, task_state="closed", merged_pr=99)
    pub = _writer(tmp_ws, c)

    first = pub.publish_role_handoff(
        handoff_id="ho-term", role="verifier", outcome_class="completed",
        task_number=TASK, pr_number=99, evidence="approved", changed="none",
    )
    assert first.verified
    posts_after_first = len(_crew_posts(c))

    # A restart reconcile + a re-publish must both dedupe to the landed comment.
    pub.reconcile()
    again = pub.publish_role_handoff(
        handoff_id="ho-term", role="verifier", outcome_class="completed",
        task_number=TASK, pr_number=99, evidence="approved", changed="none",
    )
    assert again.verified and again.deduplicated
    assert len(_crew_posts(c)) == posts_after_first == 1


def test_ambiguous_landed_post_on_closed_target_reconciles(tmp_ws: Workspace):
    """Crash-after-post-before-ack against a closed task: the create raises
    AMBIGUOUS but the comment actually landed; the writer reconciles on the marker
    and reports verified without a duplicate (no lost/duplicate record, #49)."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c, task_state="closed", merged_pr=99)
    c.ambiguous_but_landed = True
    c.fail_once("create_comment", GitHubErrorKind.AMBIGUOUS, "connection reset after write")
    pub = _writer(tmp_ws, c)

    outcome = pub.publish_role_handoff(
        handoff_id="ho-term", role="verifier", outcome_class="completed",
        task_number=TASK, pr_number=99, evidence="approved", changed="none",
    )
    assert outcome.verified and outcome.deduplicated
    assert len(_crew_posts(c)) == 1
    # A follow-up reconcile stays idempotent.
    pub.reconcile()
    assert len(_crew_posts(c)) == 1


# ── 3. WAIT-vs-PAUSE orchestrator matrix ────────────────────────────────
def test_transient_outage_waits_and_self_heals(tmp_ws: Workspace):
    """A sustained *transient* write outage leaves the run WAITING (recoverable,
    self-heals on the next run's reconcile) — never ``human-blocked`` (#49)."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    c.always_fail("create_comment", GitHubErrorKind.TIMEOUT, "outage")
    pub = _writer(tmp_ws, c)
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), wait("external")],
                        role_handoff=_material_handoff)
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.WAITING
    assert run.routing_authority == LEAD_PENDING
    assert run.human_blocker in (None, "")
    assert run.wake_condition
    assert tmp_ws.exit_reason_file.read_text().strip() == "waiting"


def test_permission_denial_pauses_as_human_blocker(tmp_ws: Workspace):
    """A genuine permission/credential/policy denial on the terminal write is the
    one case that still PAUSES for an operator (``human-blocked``)."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    c.always_fail("create_comment", GitHubErrorKind.PERMISSION, "resource not accessible")
    pub = _writer(tmp_ws, c)
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), wait("external")],
                        role_handoff=_material_handoff)
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.PAUSED
    assert run.human_blocker
    assert tmp_ws.exit_reason_file.read_text().strip() == "human-blocked"


# ── 4. end-to-end self-heal: WAIT → resume → reconcile → continue ───────
def test_outage_self_heals_on_plain_run_without_operator_mutation(tmp_ws: Workspace):
    """The canonical #49 flow: the worker's terminal handoff cannot verify during a
    transient outage → run WAITS. The operator does NOT touch the record; once the
    bus recovers a plain ``crewd run`` reconciles the reserved terminal write
    (idempotent, no duplicate) and the run continues — the handoff is consumed."""
    c = FakeGitHubClient(REPO)
    _valid_worker_record(c)
    # The Lead dispatch write (call 1) lands; the worker's terminal write fails.
    c.create_fail_after = 1
    from crewd.github_bus import GitHubError
    c.create_fail_error = GitHubError(GitHubErrorKind.TIMEOUT, "outage")
    pub = _writer(tmp_ws, c)
    fake = FakeExecutor(lead_script=[dispatch_to("worker"), wait("external")],
                        role_handoff=_material_handoff)
    orch, disp = _orch(tmp_ws, fake, pub)
    orch.run(once=False)

    run = disp.start_or_resume_run(GOAL)
    assert RunStatus(run.status) is RunStatus.WAITING
    worker_pending = [i for i in pub.store.list_pending() if i.role == "worker"]
    assert len(worker_pending) == 1
    creates_during_outage = c.create_calls
    disp.close()

    # Outage clears; a plain resume over the same durable state reconciles the
    # reserved terminal write on start (marker dedupes → no duplicate), Lead then
    # re-consumes the now-verified handoff and the run reaches its wait terminal.
    c.create_fail_after = None
    pub2 = _writer(tmp_ws, c)
    fake2 = FakeExecutor(lead_script=[wait("external")], role_handoff=_material_handoff)
    disp2 = Dispatcher(tmp_ws.state_dir / "dispatch.db")
    orch2, _ = _orch(tmp_ws, fake2, pub2, disp=disp2)
    orch2.run(once=False)

    assert pub2.counts()["pending"] == 0
    # Exactly one worker terminal comment exists (reconcile did not double-post).
    worker_posts = [p for p in _crew_posts(c) if "worker \u2192 verifier" in p.body]
    assert len(worker_posts) == 1
    assert c.create_calls == creates_during_outage + 1  # one successful reconcile write
    run = disp2.start_or_resume_run(GOAL)
    assert [h.role for h in disp2.pending_handoffs(run.id)] == []


# ── 5. diagnostics separate self-heal from operator-needed ──────────────
def test_diagnostics_flag_self_heal_vs_operator(tmp_ws: Workspace):
    """A pending write that last routed WAIT is surfaced as self-healing (no
    operator action); one that last routed PAUSE is flagged as needing an
    operator — so status does not cry "human blocker" for a transient race."""
    (tmp_ws.state_dir).mkdir(parents=True, exist_ok=True)
    store = IntentStore.for_workspace(tmp_ws)
    from crewd.public_writer import WriteIntent

    store.reserve(WriteIntent(correlation_id="w-wait", role="verifier",
                              target_role="lead", target="issue", number=TASK,
                              body="terminal review"))
    store.record_detail("w-wait", "github timeout: outage", route=Route.WAIT.value)
    store.reserve(WriteIntent(correlation_id="w-perm", role="verifier",
                              target_role="lead", target="issue", number=TASK,
                              body="terminal review"))
    store.record_detail("w-perm", "permission denied", route=Route.PAUSE.value)
    # A REJECT (invalid public record) is operator-needed too — it will not
    # reconcile on its own and must NOT be counted as self-healing.
    store.reserve(WriteIntent(correlation_id="w-reject", role="verifier",
                              target_role="lead", target="issue", number=TASK,
                              body="terminal review"))
    store.record_detail("w-reject", "invalid attribution", route=Route.REJECT.value)

    snap = build_snapshot(tmp_ws, crew_name=CREW, backend="fake", goal_label=GOAL)
    pw = snap.public_writes
    assert pw is not None
    assert pw["pending"] == 3
    assert pw["needs_operator"] == ["w-perm", "w-reject"]
    detail = {d["id"]: d for d in pw["pending_detail"]}
    assert detail["w-wait"]["route"] == "wait" and detail["w-wait"]["attempts"] == 1
    assert detail["w-perm"]["route"] == "pause"
    assert detail["w-reject"]["route"] == "reject"
    # The recovery hint names BOTH the operator-blocked writes and the self-healing
    # one, and only the former ask for operator action.
    assert snap.recovery_action
    assert "operator action" in snap.recovery_action
    assert "no operator action needed" in snap.recovery_action
    assert "w-perm" in snap.recovery_action and "w-reject" in snap.recovery_action
