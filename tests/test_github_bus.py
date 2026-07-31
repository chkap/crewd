"""Deterministic fake-GitHub coverage for the public-bus transaction boundary.

Covers (GOAL verification requirements): successful posts; missing / closed /
wrong-goal / wrong-repository / unverified references; permission, rate-limit,
timeout, and ambiguous GitHub failures with explicit wait/pause routing; retry;
crash-and-reconcile; duplicate-post suppression; and first-line attribution
render/parse/validate.
"""
from __future__ import annotations

import pytest

from crewd.github_bus import (
    Attribution,
    GitHubErrorKind,
    PublicBus,
    RejectReason,
    Route,
    classify_gh_stderr,
    correlation_marker,
    validate_attribution,
)

from fake_github import FakeGitHubClient


CREW = "crewd-refactor"
REPO = "acme/widget"
GOAL = "goal:v2"


def _bus(client: FakeGitHubClient) -> PublicBus:
    return PublicBus(client, crew=CREW, expected_repo=REPO, goal_label=GOAL)


def _seed_goal(client: FakeGitHubClient, number: int = 30) -> None:
    client.add_issue(number, "GOAL: restore public bus", labels=(GOAL,))


def _lead_assignment_body() -> str:
    return f"> **[crewd:lead -> worker]** {CREW}\n\nAssigned."


def _worker_readiness_body() -> str:
    return f"> **[crewd:worker -> verifier]** {CREW}\n\nReady: PR #99."


# ── attribution ──────────────────────────────────────────────────────────
def test_attribution_render_roundtrip():
    a = Attribution("lead", "worker", CREW)
    assert a.render() == f"> **[crewd:lead -> worker]** {CREW}"
    parsed = Attribution.parse(a.render())
    assert parsed == a


def test_attribution_parses_unicode_arrow():
    parsed = Attribution.parse(f"> **[crewd:verifier \u2192 worker]** {CREW}")
    assert parsed == Attribution("verifier", "worker", CREW)


def test_attribution_parses_first_nonempty_line_only():
    body = f"\n\n> **[crewd:worker -> all]** {CREW}\n\nrest of body"
    assert Attribution.parse(body) == Attribution("worker", "all", CREW)


def test_validate_attribution_rejects_missing_and_bad():
    assert validate_attribution("no attribution here") is not None
    assert validate_attribution(f"> **[crewd:boss -> worker]** {CREW}") is not None  # bad role
    assert validate_attribution(f"> **[crewd:lead -> nobody]** {CREW}") is not None  # bad target
    assert validate_attribution(Attribution("lead", "worker", CREW).render(), crew=CREW) is None


def test_validate_attribution_wrong_crew():
    body = Attribution("lead", "worker", "other-crew").render()
    assert validate_attribution(body, crew=CREW) is not None


# ── goal prerequisite ────────────────────────────────────────────────────
def test_goal_prereq_success():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    out = _bus(c).verify_goal_prerequisite()
    assert out.route is Route.PROCEED and out.refs["umbrella"] == 30


def test_goal_prereq_missing():
    c = FakeGitHubClient(REPO)
    out = _bus(c).verify_goal_prerequisite()
    assert out.route is Route.REJECT and out.reason is RejectReason.MISSING


def test_goal_prereq_multiple_umbrellas():
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: one", labels=(GOAL,))
    c.add_issue(31, "GOAL: two", labels=(GOAL,))
    out = _bus(c).verify_goal_prerequisite()
    assert out.route is Route.REJECT and out.reason is RejectReason.MULTIPLE


def test_goal_prereq_wrong_repo():
    c = FakeGitHubClient("someone/else")
    _seed_goal(c)
    out = _bus(c).verify_goal_prerequisite()
    assert out.route is Route.REJECT and out.reason is RejectReason.WRONG_REPO


# ── worker dispatch prerequisite ─────────────────────────────────────────
def _seed_worker_ready(c: FakeGitHubClient, task: int = 29) -> None:
    _seed_goal(c)
    c.add_issue(task, "task: public bus", labels=("crewd:task", GOAL), assignees=("alice",))


def test_worker_dispatch_success_with_assignee():
    c = FakeGitHubClient(REPO)
    _seed_worker_ready(c)
    out = _bus(c).verify_worker_dispatch(29)
    assert out.route is Route.PROCEED and out.refs["task"] == 29


def test_worker_dispatch_success_with_public_lead_assignment():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    c.add_issue(29, "task", labels=("crewd:task", GOAL))  # no assignee
    c.add_comment("issue", 29, _lead_assignment_body())   # public assignment
    out = _bus(c).verify_worker_dispatch(29)
    assert out.route is Route.PROCEED


def test_worker_dispatch_missing_task():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    out = _bus(c).verify_worker_dispatch(29)
    assert out.route is Route.REJECT and out.reason is RejectReason.MISSING


def test_worker_dispatch_closed_task():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    c.add_issue(29, "task", state="closed", labels=("crewd:task", GOAL), assignees=("a",))
    out = _bus(c).verify_worker_dispatch(29)
    assert out.route is Route.REJECT and out.reason is RejectReason.CLOSED


def test_worker_dispatch_wrong_goal_label():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    c.add_issue(29, "task", labels=("crewd:task", "goal:v1"), assignees=("a",))
    out = _bus(c).verify_worker_dispatch(29)
    assert out.route is Route.REJECT and out.reason is RejectReason.WRONG_GOAL


def test_worker_dispatch_no_owner():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    c.add_issue(29, "task", labels=("crewd:task", GOAL))  # no assignee, no assignment
    out = _bus(c).verify_worker_dispatch(29)
    assert out.route is Route.REJECT and out.reason is RejectReason.NO_ASSIGNMENT


# ── verifier dispatch prerequisite ───────────────────────────────────────
def test_verifier_dispatch_success():
    c = FakeGitHubClient(REPO)
    _seed_worker_ready(c)
    c.add_pull(31, "impl", linked_issues=(29,))
    c.add_comment("issue", 29, _worker_readiness_body())
    out = _bus(c).verify_verifier_dispatch(29)
    assert out.route is Route.PROCEED and out.refs["pr"] == 31


def test_verifier_dispatch_no_linked_pr():
    c = FakeGitHubClient(REPO)
    _seed_worker_ready(c)
    c.add_comment("issue", 29, _worker_readiness_body())
    out = _bus(c).verify_verifier_dispatch(29)
    assert out.route is Route.REJECT and out.reason is RejectReason.MISSING


def test_verifier_dispatch_no_readiness_record():
    c = FakeGitHubClient(REPO)
    _seed_worker_ready(c)
    c.add_pull(31, "impl", linked_issues=(29,))
    out = _bus(c).verify_verifier_dispatch(29)
    assert out.route is Route.REJECT and out.reason is RejectReason.NOT_READY


def test_verifier_dispatch_multiple_linked_pulls_fails_closed():
    """Several open PRs link the task and none is bound: the gate refuses to guess
    which to review, failing closed (MULTIPLE) rather than picking the first."""
    c = FakeGitHubClient(REPO)
    _seed_worker_ready(c)
    c.add_pull(31, "impl A", linked_issues=(29,))
    c.add_pull(32, "impl B", linked_issues=(29,))
    c.add_comment("issue", 29, _worker_readiness_body())
    out = _bus(c).verify_verifier_dispatch(29)
    assert out.route is Route.REJECT and out.reason is RejectReason.MULTIPLE


def test_verifier_dispatch_pinned_pr_disambiguates_multiple():
    """A bound PR pins the exact review target even when several PRs link the
    task — the arbitrary-first ambiguity is resolved to the authoritative PR."""
    c = FakeGitHubClient(REPO)
    _seed_worker_ready(c)
    c.add_pull(31, "impl A", linked_issues=(29,))
    c.add_pull(32, "impl B", linked_issues=(29,))
    c.add_comment("issue", 29, _worker_readiness_body())
    out = _bus(c).verify_verifier_dispatch(29, pr_number=32)
    assert out.route is Route.PROCEED and out.refs["pr"] == 32


def test_verifier_dispatch_pinned_pr_not_linked_fails_closed():
    """A bound PR that is not an open linked PR (unrelated/stale) fails closed
    rather than silently reviewing some other linked PR."""
    c = FakeGitHubClient(REPO)
    _seed_worker_ready(c)
    c.add_pull(31, "impl", linked_issues=(29,))
    c.add_comment("issue", 29, _worker_readiness_body())
    out = _bus(c).verify_verifier_dispatch(29, pr_number=999)
    assert out.route is Route.REJECT and out.reason is RejectReason.MISSING


# ── finish prerequisite ──────────────────────────────────────────────────
def test_finish_success():
    c = FakeGitHubClient(REPO)
    c.add_issue(40, "final acceptance", state="closed")
    c.add_comment("issue", 40, f"> **[crewd:lead -> all]** {CREW}\n\nGoal complete summary.")
    out = _bus(c).verify_finish(40)
    assert out.route is Route.PROCEED and out.refs["acceptance"] == 40


def test_finish_rejects_open_acceptance():
    c = FakeGitHubClient(REPO)
    c.add_issue(40, "final acceptance", state="open")
    out = _bus(c).verify_finish(40)
    assert out.route is Route.REJECT and out.reason is RejectReason.NOT_READY


def test_finish_rejects_missing_summary():
    c = FakeGitHubClient(REPO)
    c.add_issue(40, "final acceptance", state="closed")
    out = _bus(c).verify_finish(40)
    assert out.route is Route.REJECT and out.reason is RejectReason.MISSING


# ── GitHub failure routing (never silent internal-only success) ──────────
def test_permission_error_routes_to_pause():
    c = FakeGitHubClient(REPO)
    c.fail_once("list_issues", GitHubErrorKind.PERMISSION, "403")
    out = _bus(c).verify_goal_prerequisite()
    assert out.route is Route.PAUSE and out.error_kind is GitHubErrorKind.PERMISSION


@pytest.mark.parametrize("kind", [
    GitHubErrorKind.RATE_LIMIT, GitHubErrorKind.TIMEOUT, GitHubErrorKind.TRANSIENT,
])
def test_transient_errors_route_to_wait(kind):
    c = FakeGitHubClient(REPO)
    c.fail_once("list_issues", kind, "boom")
    out = _bus(c).verify_goal_prerequisite()
    assert out.route is Route.WAIT and out.error_kind is kind


def test_error_during_worker_task_lookup_routes_wait():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    c.fail_once("get_issue", GitHubErrorKind.TIMEOUT, "slow")
    out = _bus(c).verify_worker_dispatch(29)
    assert out.route is Route.WAIT


# ── idempotent posting ───────────────────────────────────────────────────
def _post(bus: PublicBus, cid: str = "corr-1"):
    client = bus.client
    if isinstance(client, FakeGitHubClient) and client.get_issue(29) is None:
        client.add_issue(29, "task")
    return bus.post(role="worker", target_role="verifier",
                    body="ready for review", target="issue", number=29,
                    correlation_id=cid)


def test_post_success_attributes_and_marks():
    c = FakeGitHubClient(REPO)
    out = _post(_bus(c))
    assert out.route is Route.PROCEED and not out.deduplicated
    body = c.comments[("issue", 29)][0].body
    assert body.startswith(f"> **[crewd:worker -> verifier]** {CREW}")
    assert correlation_marker("corr-1") in body


def test_post_is_idempotent_no_double_post():
    c = FakeGitHubClient(REPO)
    bus = _bus(c)
    first = _post(bus)
    second = _post(bus)  # retry with same correlation id
    assert first.route is Route.PROCEED
    assert second.route is Route.PROCEED and second.deduplicated
    assert len(c.comments[("issue", 29)]) == 1  # exactly one comment


def test_post_permission_error_pauses():
    c = FakeGitHubClient(REPO)
    c.fail_once("find_comment", GitHubErrorKind.PERMISSION, "403")
    out = _post(_bus(c))
    assert out.route is Route.PAUSE


def test_post_rate_limit_waits_and_does_not_post():
    c = FakeGitHubClient(REPO)
    c.fail_once("create_comment", GitHubErrorKind.RATE_LIMIT, "slow down")
    out = _post(_bus(c))
    assert out.route is Route.WAIT
    assert ("issue", 29) not in c.comments or not c.comments[("issue", 29)]


def test_post_ambiguous_write_that_landed_reconciles_without_dup():
    c = FakeGitHubClient(REPO)
    c.ambiguous_but_landed = True
    c.fail_once("create_comment", GitHubErrorKind.AMBIGUOUS, "unknown result")
    bus = _bus(c)
    out = _post(bus)
    # Reconciliation finds the marker → proceed, deduplicated, single comment.
    assert out.route is Route.PROCEED and out.deduplicated
    assert len(c.comments[("issue", 29)]) == 1
    # A retry still does not double-post.
    retry = _post(bus)
    assert retry.deduplicated and len(c.comments[("issue", 29)]) == 1


def test_post_ambiguous_write_that_did_not_land_waits():
    c = FakeGitHubClient(REPO)
    c.ambiguous_but_landed = False
    c.fail_once("create_comment", GitHubErrorKind.AMBIGUOUS, "unknown result")
    out = _post(_bus(c))
    assert out.route is Route.WAIT
    assert ("issue", 29) not in c.comments or not c.comments[("issue", 29)]


def test_post_retry_after_transient_failure_succeeds_once():
    c = FakeGitHubClient(REPO)
    c.fail_once("create_comment", GitHubErrorKind.TIMEOUT, "t/o")
    bus = _bus(c)
    first = _post(bus)
    assert first.route is Route.WAIT and c.create_calls == 1
    second = _post(bus)  # retry
    assert second.route is Route.PROCEED
    assert len(c.comments[("issue", 29)]) == 1


def test_post_rejects_invalid_attribution_target():
    c = FakeGitHubClient(REPO)
    out = _bus(c).post(role="worker", target_role="nobody", body="x",
                       target="issue", number=29, correlation_id="z")
    assert out.route is Route.REJECT


# ── stderr classification ────────────────────────────────────────────────
@pytest.mark.parametrize("stderr,expected", [
    ("API rate limit exceeded", GitHubErrorKind.RATE_LIMIT),
    ("request timed out", GitHubErrorKind.TIMEOUT),
    ("HTTP 404: Not Found", GitHubErrorKind.NOT_FOUND),
    ("HTTP 403: Resource not accessible", GitHubErrorKind.PERMISSION),
    ("some random network blip", GitHubErrorKind.TRANSIENT),
])
def test_classify_gh_stderr(stderr, expected):
    assert classify_gh_stderr(stderr, 1) is expected


# ── typed reference resolution (public record → active identifiers) ──
def test_resolve_active_task_single():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    c.add_issue(29, "task", labels=("crewd:task", GOAL))
    c.add_comment("issue", 29, _lead_assignment_body())
    out = _bus(c).resolve_active_task()
    assert out.ok
    assert out.refs["task"] == 29


def test_resolve_active_task_none_assigned():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    c.add_issue(29, "task", labels=("crewd:task", GOAL))  # no assignment comment
    out = _bus(c).resolve_active_task()
    assert not out.ok
    assert out.reason is RejectReason.NO_ASSIGNMENT


def test_resolve_active_task_ambiguous():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    for n in (29, 28):
        c.add_issue(n, "task", labels=("crewd:task", GOAL))
        c.add_comment("issue", n, _lead_assignment_body())
    out = _bus(c).resolve_active_task()
    assert not out.ok
    assert out.reason is RejectReason.MULTIPLE


def test_resolve_active_task_ignores_other_goal():
    c = FakeGitHubClient(REPO)
    _seed_goal(c)
    c.add_issue(29, "task", labels=("crewd:task", "goal:v1"))  # wrong goal
    c.add_comment("issue", 29, _lead_assignment_body())
    out = _bus(c).resolve_active_task()
    assert not out.ok
    assert out.reason is RejectReason.NO_ASSIGNMENT


def test_resolve_active_task_propagates_goal_failure():
    c = FakeGitHubClient(REPO)  # no umbrella at all
    out = _bus(c).resolve_active_task()
    assert not out.ok
    assert out.reason is RejectReason.MISSING


def test_resolve_acceptance_issue_finds_closed_umbrella():
    c = FakeGitHubClient(REPO)
    c.add_issue(30, "GOAL: x", state="closed", labels=(GOAL,))
    out = _bus(c).resolve_acceptance_issue()
    assert out.ok
    assert out.refs["acceptance"] == 30


def test_resolve_acceptance_issue_missing():
    c = FakeGitHubClient(REPO)
    out = _bus(c).resolve_acceptance_issue()
    assert not out.ok
    assert out.reason is RejectReason.MISSING


def test_resolve_acceptance_issue_transient_waits():
    c = FakeGitHubClient(REPO)
    c.fail_once("list_issues", GitHubErrorKind.TIMEOUT, "deadline")
    out = _bus(c).resolve_acceptance_issue()
    assert out.route is Route.WAIT
