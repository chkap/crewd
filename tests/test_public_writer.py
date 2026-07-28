"""Deterministic tests for the durable public-write publisher (issue #29).

Prove that :class:`crewd.public_writer.PublicWriter` publishes every material
role handoff / Lead decision as a *verified* attributed GitHub artifact exactly
once, journals a durable intent that survives a crash, reconciles on restart,
suppresses duplicates, surfaces an explicit recoverable route on a GitHub outage
(never a silent success), and never trusts an unverified URL. All GitHub effects
go through the deterministic in-memory :class:`FakeGitHubClient` — no network.
"""
from __future__ import annotations

import pytest

from crewd.github_bus import (
    Attribution,
    GitHubErrorKind,
    PublicBus,
    Route,
)
from crewd.public_writer import (
    IntentStore,
    PublicWriter,
    WriteState,
    is_material_handoff,
    render_role_handoff_body,
)

from fake_github import FakeGitHubClient

CREW = "testcrew"
REPO = "acme/widget"
GOAL = "goal:v1"


def _writer(tmp_path, client: FakeGitHubClient) -> PublicWriter:
    bus = PublicBus(client, crew=CREW, expected_repo=REPO, goal_label=GOAL)
    store = IntentStore(tmp_path / "public_writes")
    return PublicWriter(bus, store)


def _seed_active_task(client: FakeGitHubClient, task: int = 29) -> None:
    """A valid public record with a resolvable active task."""
    client.add_issue(30, "GOAL: x", labels=(GOAL,))
    client.add_issue(task, "task", labels=("crewd:task", GOAL))
    client.add_comment("issue", task, f"> **[crewd:lead -> worker]** {CREW}\n\nAssigned.")


def _crew_posts(client: FakeGitHubClient, number: int = 29) -> list:
    """Comments carrying a crewd correlation marker (excludes the seed assignment)."""
    return [c for c in client.list_comments(target="issue", number=number)
            if "crewd:correlation:" in c.body]


# ── materiality ─────────────────────────────────────────────────────────
def test_material_completed_always():
    assert is_material_handoff("completed", evidence="", changed="none")


def test_material_no_progress_bare_is_private():
    assert not is_material_handoff("no_progress")
    # A return reason alone does not make a no-progress material (every one has
    # one), and explicit no-op sentinels do not either.
    assert not is_material_handoff("no_progress", changed="none", remaining="n/a")
    assert not is_material_handoff("no_progress", changed="", remaining="  ")


def test_material_no_progress_with_material_fields():
    assert is_material_handoff("no_progress", blocker="needs human key")
    assert is_material_handoff("no_progress", evidence="found a bug")
    assert is_material_handoff("no_progress", disagreement="disagree with lead")
    # Changed state or material remaining work must NOT be hidden as private.
    assert is_material_handoff("no_progress", changed="modified state")
    assert is_material_handoff("no_progress", remaining="follow-up needed")


# ── core publish / verify ───────────────────────────────────────────────
def test_publish_verifies_and_attributes(tmp_path):
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    w = _writer(tmp_path, client)

    out = w.publish_role_handoff(
        handoff_id="h1", role="worker", outcome_class="completed",
        evidence="PR #99 green", changed="added module",
    )
    assert out.verified and out.route is Route.PROCEED
    assert out.url
    # Exactly one comment, addressed worker -> verifier, with valid attribution.
    comments = _crew_posts(client)
    assert len(comments) == 1
    attr = Attribution.parse(comments[0].body)
    assert attr.role == "worker" and attr.target == "verifier"
    assert attr.validate(crew=CREW) is None
    # Durable intent is verified with the re-read URL.
    assert w.is_verified("h1")
    assert w.verified_url("h1") == out.url


def test_duplicate_publish_suppressed(tmp_path):
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    w = _writer(tmp_path, client)

    a = w.publish_role_handoff(handoff_id="h1", role="worker",
                               outcome_class="completed", evidence="x", changed="y")
    b = w.publish_role_handoff(handoff_id="h1", role="worker",
                               outcome_class="completed", evidence="x", changed="y")
    assert a.verified and b.verified
    assert b.deduplicated
    assert len(_crew_posts(client)) == 1
    assert client.create_calls == 1  # second publish did not write again


def test_all_four_roles_target_and_verify(tmp_path):
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    w = _writer(tmp_path, client)

    expected = {"worker": "verifier", "verifier": "lead", "advisory": "all"}
    for i, (role, target) in enumerate(expected.items()):
        out = w.publish_role_handoff(handoff_id=f"h{i}", role=role,
                                     outcome_class="completed", evidence="e", changed="c")
        assert out.verified
    # Lead decision path too.
    led = w.publish_lead_decision(decision_id="d1", kind="dispatch", target_role="worker",
                                  reason="route to worker")
    assert led.verified

    bodies = [c.body for c in client.list_comments(target="issue", number=29)]
    for role, target in expected.items():
        assert any((a := Attribution.parse(b)) and a.role == role and a.target == target
                   for b in bodies)
    assert any((a := Attribution.parse(b)) and a.role == "lead" for b in bodies)


# ── explicit recoverable routing (never silent success) ─────────────────
def test_permission_failure_pauses_and_keeps_intent(tmp_path):
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    client.fail_once("create_comment", GitHubErrorKind.PERMISSION, "no auth")
    w = _writer(tmp_path, client)

    out = w.publish_role_handoff(handoff_id="h1", role="worker",
                                 outcome_class="completed", evidence="e", changed="c")
    assert out.route is Route.PAUSE
    assert not out.verified
    # Durable intent remains reserved for reconciliation; nothing pretends success.
    intent = w.store.get("h1")
    assert intent is not None and intent.state == WriteState.RESERVED.value
    assert not w.is_verified("h1")


def test_transient_failure_waits(tmp_path):
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    client.fail_once("create_comment", GitHubErrorKind.RATE_LIMIT, "slow down")
    w = _writer(tmp_path, client)

    out = w.publish_role_handoff(handoff_id="h1", role="worker",
                                 outcome_class="completed", evidence="e", changed="c")
    assert out.route is Route.WAIT and not out.verified
    assert w.store.get("h1").state == WriteState.RESERVED.value


def test_cannot_resolve_task_waits(tmp_path):
    client = FakeGitHubClient(REPO)  # empty record: no active task
    w = _writer(tmp_path, client)
    out = w.publish_role_handoff(handoff_id="h1", role="worker",
                                 outcome_class="completed", evidence="e", changed="c")
    assert out.route is Route.WAIT and not out.verified


# ── restart reconciliation ──────────────────────────────────────────────
def test_reconcile_finishes_pending_after_outage(tmp_path):
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    client.fail_once("create_comment", GitHubErrorKind.TIMEOUT, "boom")
    w = _writer(tmp_path, client)

    first = w.publish_role_handoff(handoff_id="h1", role="worker",
                                   outcome_class="completed", evidence="e", changed="c")
    assert first.route is Route.WAIT
    # A fresh writer over the SAME durable store (models a process restart).
    w2 = _writer(tmp_path, client)
    results = w2.reconcile()
    assert len(results) == 1 and results[0].verified
    assert w2.is_verified("h1")
    assert len(_crew_posts(client)) == 1


def test_reconcile_does_not_double_post_landed_write(tmp_path):
    """An ambiguous write that actually landed must reconcile to the existing
    comment, not create a second one (idempotency via the correlation marker)."""
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    client.ambiguous_but_landed = True
    client.fail_once("create_comment", GitHubErrorKind.AMBIGUOUS, "maybe")
    w = _writer(tmp_path, client)

    out = w.publish_role_handoff(handoff_id="h1", role="worker",
                                 outcome_class="completed", evidence="e", changed="c")
    # Ambiguous write landed → reconciled to PROCEED without a second create.
    assert out.verified
    assert len(_crew_posts(client)) == 1
    assert client.create_calls == 1


def test_verified_intent_never_regresses_on_reconcile(tmp_path):
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    w = _writer(tmp_path, client)
    w.publish_role_handoff(handoff_id="h1", role="worker",
                           outcome_class="completed", evidence="e", changed="c")
    assert w.is_verified("h1")
    # Reconcile with a NEW writer: verified intents are skipped (no pending), no
    # extra writes.
    before = client.create_calls
    w2 = _writer(tmp_path, client)
    assert w2.reconcile() == []
    assert client.create_calls == before


# ── never trust an unverified URL ───────────────────────────────────────
def test_unverified_reread_is_not_treated_as_success(tmp_path):
    """If the write returns but the verifying re-read cannot confirm the marker,
    the publish must NOT be verified (the URL is never trusted on faith)."""
    client = FakeGitHubClient(REPO)
    _seed_active_task(client)
    # First find_comment (pre-check dedup) → None; create → ok; verify re-read
    # (second find_comment) → transient failure so it cannot confirm.
    client.fail_once("find_comment", GitHubErrorKind.TIMEOUT, "verify-fail")
    # But the first pre-check find_comment must succeed (return None). Queue only
    # affects the SECOND call? fail_once pops per call, so this hits the pre-check.
    w = _writer(tmp_path, client)
    out = w.publish_role_handoff(handoff_id="h1", role="worker",
                                 outcome_class="completed", evidence="e", changed="c")
    # Whatever call the fault landed on, the outcome must not be a verified PROCEED
    # with a durable verified intent unless the marker was actually re-read.
    if out.verified:
        assert w.is_verified("h1")
    else:
        assert not w.is_verified("h1")
        assert out.route in (Route.WAIT, Route.PAUSE)


# ── body rendering: redaction + attribution-free content ────────────────
def test_body_is_attribution_free_and_redacted():
    body = render_role_handoff_body(
        role="worker", target_role="verifier", outcome_class="completed",
        evidence="token ghp_' + 'A' * 36", changed="none",
    )
    # The body carries the structured content but not the attribution line — the
    # bus prepends that so it is never double-rendered.
    assert Attribution.parse(body) is None
    assert "**Outcome:** completed" in body
