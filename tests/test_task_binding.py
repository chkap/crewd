"""Exact-task-binding regressions for the dispatch → handoff → Verifier boundary (#47).

The defect (#47): the public-bus gate and writer re-resolved a *global* active
task from attributed Lead assignment comments instead of carrying the exact
routed task identity through the accepted Lead decision, the dispatch/attempt/
handoff journal, and Verifier routing. With more than one assigned task the
global census is ambiguous, so a role handoff was published to the wrong task
(live goal:v3: the Worker handoff for #44/PR #46 landed on the audit task #43,
and Verifier routing then paused with ``no open PR linked to task #43``).

These deterministic tests (no SDK, no network) lock the binding end to end:

* the accepted Lead decision carries a verified immutable task reference;
* that reference is persisted with the dispatch and recoverable for any handoff;
* the gate and writer validate/publish against the *bound* task, never a later
  global census, even when multiple tasks are assigned;
* the binding survives a restart between dispatch and handoff and is idempotent;
* a stale/closed task fails before role execution with a recoverable route and
  does not demand an unrelated PR; and
* generated attribution always matches the parser's canonical contract.
"""
from __future__ import annotations

import sqlite3

import pytest

from crewd.dispatcher import (
    Dispatcher,
    DecisionKind,
    LeadDecision,
    RunStatus,
    LEAD_PENDING,
)
from crewd.executor import parse_lead_decision
from crewd.github_bus import (
    Attribution,
    PublicBus,
    PublicBusGate,
    RejectReason,
    Route,
    _ATTR_RE,
    _ATTR_RENDER,
    validate_attribution,
)
from crewd.public_writer import IntentStore, PublicWriter
from crewd.orchestrator import Orchestrator
from crewd.session_backend import AttemptOutcome

CREW = "testcrew"
REPO = "acme/widget"
GOAL = "goal:v3"
ROLES = ("lead", "advisory", "worker", "verifier")


# ─────────────────────────── fixtures / helpers ───────────────────────────
def _open(tmp_path) -> Dispatcher:
    return Dispatcher(tmp_path / "dispatch.sqlite3")


def _bus(client) -> PublicBus:
    return PublicBus(client, crew=CREW, expected_repo=REPO, goal_label=GOAL)


def _writer(tmp_path, client) -> PublicWriter:
    return PublicWriter(_bus(client), IntentStore(tmp_path / "public_writes"))


def _assign(client, task: int) -> None:
    """A public Lead assignment record for ``task`` (canonical attribution)."""
    client.add_comment("issue", task, f"> **[crewd:lead -> worker]** {CREW}\n\nAssigned.")


def _seed_task(client, task: int, *, state: str = "open", assign: bool = True) -> None:
    client.add_issue(task, f"task {task}", state=state, labels=("crewd:task", GOAL))
    if assign:
        _assign(client, task)


def _seed_umbrella(client, number: int = 100) -> None:
    client.add_issue(number, "GOAL: x", labels=(GOAL,))


def _dispatch_worker(disp, run_id, task_number, *, ack=()):
    """Apply a Lead dispatch to worker bound to ``task_number``; return the view."""
    res = disp.lead_decide(
        run_id, LeadDecision.dispatch("worker", ack=tuple(ack), task_number=task_number),
        configured_roles=ROLES,
    )
    return res.dispatch


# ══════════════════════════ 1. decision carries the task ══════════════════
def test_lead_decision_dataclass_carries_task_number():
    d = LeadDecision.dispatch("worker", task_number=44)
    assert d.kind is DecisionKind.DISPATCH
    assert d.task_number == 44


def test_parse_lead_decision_reads_task_number():
    d = parse_lead_decision({"kind": "dispatch", "role": "worker", "task_number": 44})
    assert d.task_number == 44
    # A hash-prefixed string form is coerced too (untrusted payload shape).
    d2 = parse_lead_decision({"kind": "dispatch", "role": "worker", "task_number": "#44"})
    assert d2.task_number == 44


def test_parse_lead_decision_absent_task_is_none():
    d = parse_lead_decision({"kind": "dispatch", "role": "worker"})
    assert d.task_number is None


def test_parse_lead_decision_rejects_malformed_task_number():
    with pytest.raises(ValueError):
        parse_lead_decision({"kind": "dispatch", "role": "worker", "task_number": "not-a-number"})
    with pytest.raises(ValueError):
        parse_lead_decision({"kind": "dispatch", "role": "worker", "task_number": True})


def test_non_dispatch_decisions_carry_no_task():
    assert parse_lead_decision({"kind": "wait", "wake_condition": "x"}).task_number is None
    assert parse_lead_decision({"kind": "pause", "human_blocker": "x"}).task_number is None


def test_parse_lead_decision_rejects_model_selected_continue_lead():
    # continue_lead was removed from the Lead decision contract (#65): a Lead turn
    # can no longer self-loop as a routing outcome. Rejecting it here makes the
    # executor treat it as a no-decision, routing into host-managed re-solicitation.
    with pytest.raises(ValueError):
        parse_lead_decision({"kind": "continue_lead"})


# ══════════════════════════ 2. journal persists the binding ═══════════════
def test_dispatch_persists_task_number(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    dsp = _dispatch_worker(disp, run.id, 44)
    assert dsp.task_number == 44
    # Re-read via get_dispatch (durable, not just the in-memory view).
    assert disp.get_dispatch(dsp.id).task_number == 44


def test_task_number_recoverable_for_any_handoff(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    dsp = _dispatch_worker(disp, run.id, 44)
    att = disp.reserve_attempt(run.id, dsp.id, "worker")
    disp.mark_started(att, session_id="s", generation=0)
    hid = disp.record_terminal(att, AttemptOutcome.IDLE_COMPLETED, evidence="PR #46")
    # The handoff resolves back to the *exact* routed task via attempt → dispatch.
    assert disp.task_number_for_handoff(hid) == 44


def test_task_number_for_unbound_or_unknown_handoff_is_none(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    # A dispatch with no bound task (legacy / non-task role) yields None, so the
    # writer surfaces a recoverable route instead of guessing.
    res = disp.lead_decide(run.id, LeadDecision.dispatch("worker"), configured_roles=ROLES)
    att = disp.reserve_attempt(run.id, res.dispatch.id, "worker")
    disp.mark_started(att, session_id="s", generation=0)
    hid = disp.record_terminal(att, AttemptOutcome.IDLE_COMPLETED, evidence="x")
    assert disp.task_number_for_handoff(hid) is None
    assert disp.task_number_for_handoff("ho-does-not-exist") is None


def test_pr_bound_to_task_inherited_across_dispatches(tmp_path):
    """A PR bound to one dispatch for a task (Worker routing, or host-recovered
    from the #64 lost-handoff chain) is the authoritative review target for a
    later Verifier dispatch on the same task (#47/#65)."""
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    assert disp.pr_bound_to_task(44) is None  # nothing bound yet
    dsp = _dispatch_worker(disp, run.id, 44)
    disp.bind_pr_to_dispatch(dsp.id, 46)
    # A later dispatch for the same task inherits the exact bound PR.
    assert disp.pr_bound_to_task(44) == 46


def test_pr_bound_to_task_fails_closed_on_inconsistent_bindings(tmp_path):
    """Inconsistent PR bindings across dispatches for one task resolve to None so
    the caller falls back to the public-record gate rather than guessing."""
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    d1 = _dispatch_worker(disp, run.id, 44)
    disp.bind_pr_to_dispatch(d1.id, 46)
    # Return authority to Lead, then a second dispatch for the same task bound to
    # a different PR (an anomaly) makes the task-level lookup ambiguous.
    att = disp.reserve_attempt(run.id, d1.id, "worker")
    disp.mark_started(att, session_id="s", generation=0)
    disp.record_terminal(att, AttemptOutcome.IDLE_COMPLETED, evidence="x")
    d2 = _dispatch_worker(disp, run.id, 44)
    disp.bind_pr_to_dispatch(d2.id, 47)
    assert disp.pr_bound_to_task(44) is None


# ══════════════════════════ 3. schema migration ══════════════════════════
def test_migration_adds_task_number_to_legacy_dispatch(tmp_path):
    """A pre-#47 database (dispatch without ``task_number``) is upgraded in place
    and remains fully usable, preserving the binding for new dispatches."""
    db = tmp_path / "dispatch.sqlite3"
    disp = _open(tmp_path)
    disp.start_or_resume_run(GOAL)
    disp.close()
    # Simulate the pre-#47 schema by dropping the column the migration adds.
    raw = sqlite3.connect(str(db))
    raw.execute("ALTER TABLE dispatch DROP COLUMN task_number")
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    cols = {r[1] for r in raw.execute("PRAGMA table_info(dispatch)").fetchall()}
    raw.close()
    assert "task_number" not in cols  # precondition: legacy shape

    # Reopening runs the idempotent migration.
    disp2 = _open(tmp_path)
    cols2 = {r[1] for r in disp2._conn.execute("PRAGMA table_info(dispatch)").fetchall()}
    assert "task_number" in cols2
    run = disp2.start_or_resume_run(GOAL)
    dsp = _dispatch_worker(disp2, run.id, 44)
    assert disp2.get_dispatch(dsp.id).task_number == 44


# ══════════════════════════ 4. gate uses the bound task ═══════════════════
def test_gate_validates_bound_task_ignoring_ambiguous_census(tmp_path):
    """Two assigned tasks make the global census ambiguous (MULTIPLE), but the
    gate validates the *bound* task and proceeds."""
    client = _seed_multi(tmp_path)
    gate = PublicBusGate(_bus(client))
    # Sanity: the global census the old code used is ambiguous.
    assert _bus(client).resolve_active_task().reason is RejectReason.MULTIPLE
    # Bound to #44 (has an open PR + readiness) → verifier proceeds.
    out = gate.evaluate("verifier", None, task_number=44)
    assert out.route is Route.PROCEED
    assert out.refs["task"] == 44
    assert out.refs["pr"] == 46


def test_orchestrator_persists_pr_returned_by_verifier_gate(tmp_path):
    client = _seed_multi(tmp_path)
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    dsp = disp.lead_decide(
        run.id,
        LeadDecision.dispatch("verifier", task_number=44),
        configured_roles=ROLES,
    ).dispatch
    orch = object.__new__(Orchestrator)
    orch._bus_gate = PublicBusGate(_bus(client))
    orch.disp = disp

    assert orch._bus_gate_ok(run.id, "verifier", dsp)
    assert disp.get_dispatch(dsp.id).pr_number == 46


def test_gate_rejects_when_bound_task_lacks_pr_even_if_other_task_has_one(tmp_path):
    """The live #47 repro: routing Verifier bound to the audit task #43 must fail
    against #43 — not silently pass because *some other* task (#44) has a PR."""
    client = _seed_multi(tmp_path)
    gate = PublicBusGate(_bus(client))
    out = gate.evaluate("verifier", None, task_number=43)
    assert out.route is Route.REJECT
    assert "#43" in out.detail  # names the routed task, not #44


def _seed_multi(tmp_path):
    """Two assigned tasks under the goal: #43 (audit, no PR) and #44 (impl, PR #46
    + Worker readiness) — reproducing the ambiguous public record."""
    from fake_github import FakeGitHubClient

    client = FakeGitHubClient(REPO)
    _seed_umbrella(client)
    _seed_task(client, 43)
    _seed_task(client, 44)
    client.add_pull(46, "impl", linked_issues=(44,))
    client.add_comment("issue", 44, f"> **[crewd:worker -> verifier]** {CREW}\n\nPR ready.")
    return client


def test_gate_uses_persisted_dispatch_binding_when_no_explicit_task(tmp_path):
    """After a restart the gate is consulted with the resurrected dispatch; its
    persisted ``task_number`` preserves the binding (no explicit arg needed)."""
    client = _seed_multi(tmp_path)
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    dsp = _dispatch_worker(disp, run.id, 44)
    # Re-read from the journal (models the post-restart dispatch view).
    resurrected = disp.get_dispatch(dsp.id)
    gate = PublicBusGate(_bus(client))
    out = gate.evaluate("worker", resurrected)  # no task_number kwarg
    assert out.route is Route.PROCEED
    assert out.refs["task"] == 44


# ══════════════════════════ 5. stale / closed task ═══════════════════════
def test_gate_rejects_stale_closed_bound_task(tmp_path):
    from fake_github import FakeGitHubClient

    client = FakeGitHubClient(REPO)
    _seed_umbrella(client)
    _seed_task(client, 44, state="closed")
    gate = PublicBusGate(_bus(client))
    out = gate.evaluate("worker", None, task_number=44)
    assert out.route is Route.REJECT
    assert out.reason is RejectReason.CLOSED
    # Recoverable, and it never demands an unrelated PR.
    assert "#44" in out.detail
    assert "PR" not in out.detail


# ══════════════════════════ 6. writer publishes to the bound task ═════════
def test_writer_publishes_to_bound_task_not_census(tmp_path):
    """A role handoff bound to #44 posts to #44 even though the global census is
    ambiguous — so the handoff is never diverted to another assigned task."""
    client = _seed_multi(tmp_path)
    w = _writer(tmp_path, client)
    out = w.publish_role_handoff(
        handoff_id="ho-1", role="worker", outcome_class="completed",
        task_number=44, evidence="PR #46 green", changed="added contract",
    )
    assert out.verified and out.route is Route.PROCEED
    # The artifact landed on #44 (and only #44).
    posted_44 = [c for c in client.list_comments(target="issue", number=44)
                 if "crewd:correlation:" in c.body]
    posted_43 = [c for c in client.list_comments(target="issue", number=43)
                 if "crewd:correlation:" in c.body]
    assert len(posted_44) == 1
    assert posted_43 == []


def test_writer_bound_publish_is_idempotent(tmp_path):
    client = _seed_multi(tmp_path)
    w = _writer(tmp_path, client)
    a = w.publish_role_handoff(handoff_id="ho-1", role="worker",
                               outcome_class="completed", task_number=44,
                               evidence="e", changed="c")
    b = w.publish_role_handoff(handoff_id="ho-1", role="worker",
                               outcome_class="completed", task_number=44,
                               evidence="e", changed="c")
    assert a.verified and b.verified and b.deduplicated
    assert client.create_calls == 1


def test_writer_unbound_and_ambiguous_surfaces_recoverable_route(tmp_path):
    """Without a binding, an ambiguous census must not mispublish: the writer
    returns a recoverable WAIT and writes nothing."""
    client = _seed_multi(tmp_path)
    w = _writer(tmp_path, client)
    out = w.publish_role_handoff(handoff_id="ho-1", role="worker",
                                 outcome_class="completed", evidence="e", changed="c")
    assert out.route is Route.WAIT
    assert not out.verified
    assert client.create_calls == 0


# ══════════════════════════ 7. restart between dispatch and handoff ═══════
def test_binding_survives_restart_between_dispatch_and_handoff(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    dsp = _dispatch_worker(disp, run.id, 44)
    att = disp.reserve_attempt(run.id, dsp.id, "worker")
    disp.mark_started(att, session_id="s", generation=0)
    disp.close()  # crash before the terminal handoff

    # Reopen: the persisted dispatch still carries #44.
    disp2 = _open(tmp_path)
    assert disp2.get_dispatch(dsp.id).task_number == 44
    hid = disp2.record_terminal(att, AttemptOutcome.IDLE_COMPLETED, evidence="PR #46")
    assert disp2.task_number_for_handoff(hid) == 44


def test_verifier_pr_binding_is_immutable_and_survives_handoff(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)
    dsp = disp.lead_decide(
        run.id,
        LeadDecision.dispatch("verifier", task_number=44),
        configured_roles=ROLES,
    ).dispatch
    bound = disp.bind_pr_to_dispatch(dsp.id, 46)
    assert bound.pr_number == 46
    assert disp.bind_pr_to_dispatch(dsp.id, 46).pr_number == 46

    from crewd.dispatcher import DecisionError
    import pytest

    with pytest.raises(DecisionError, match="cannot rebind"):
        disp.bind_pr_to_dispatch(dsp.id, 99)

    att = disp.reserve_attempt(run.id, dsp.id, "verifier")
    hid = disp.record_terminal(att, AttemptOutcome.IDLE_COMPLETED, evidence="accepted")
    assert disp.binding_for_handoff(hid) == (44, 46)


# ══════════════════════════ 8. advisory-then-worker (no cross-task) ═══════
def test_advisory_on_one_task_then_worker_on_another_stay_distinct(tmp_path):
    disp = _open(tmp_path)
    run = disp.start_or_resume_run(GOAL)

    # Advisory consulted on task #50.
    adv = disp.lead_decide(
        run.id, LeadDecision.dispatch("advisory", task_number=50), configured_roles=ROLES
    ).dispatch
    a_att = disp.reserve_attempt(run.id, adv.id, "advisory")
    disp.mark_started(a_att, session_id="s1", generation=0)
    a_ho = disp.record_terminal(a_att, AttemptOutcome.IDLE_COMPLETED, evidence="advice")

    # Then Worker dispatched on a *different* task #44, acking the advisory handoff.
    wrk = _dispatch_worker(disp, run.id, 44, ack=(a_ho,))
    w_att = disp.reserve_attempt(run.id, wrk.id, "worker")
    disp.mark_started(w_att, session_id="s2", generation=0)
    w_ho = disp.record_terminal(w_att, AttemptOutcome.IDLE_COMPLETED, evidence="PR #46")

    assert disp.task_number_for_handoff(a_ho) == 50
    assert disp.task_number_for_handoff(w_ho) == 44


# ══════════════════════════ 9. canonical attribution contract ════════════
def test_generated_attribution_matches_parser_canonical_contract():
    rendered = _ATTR_RENDER.format(role="lead", target="worker", crew=CREW)
    assert _ATTR_RE.match(rendered) is not None
    parsed = Attribution.parse(rendered)
    assert parsed is not None
    assert (parsed.role, parsed.target, parsed.crew) == ("lead", "worker", CREW)
    # The canonical render uses the ASCII arrow and the crewd: prefix.
    assert "crewd:lead -> worker" in rendered


def test_render_roundtrips_through_parser_for_every_role():
    for role in ("lead", "worker", "verifier", "advisory"):
        for target in ("lead", "worker", "verifier", "advisory", "all"):
            attr = Attribution(role=role, target=target, crew=CREW)
            back = Attribution.parse(attr.render())
            assert back == attr


def test_non_canonical_first_lines_are_rejected():
    # Missing the crewd: prefix (the live non-canonical form) is not an attribution.
    assert Attribution.parse(f"> **[lead -> worker]** {CREW}") is None
    assert validate_attribution(f"> **[lead -> worker]** {CREW}") is not None


def test_prompt_template_only_uses_canonical_attribution():
    """The agent-facing prompt must teach the exact canonical form the parser
    recognizes (crewd: prefix, ASCII arrow) so rendered output and parser agree."""
    from pathlib import Path

    import crewd

    tpl = (
        Path(crewd.__file__).parent
        / "templates" / "agents" / "_comm_attribution.md.j2"
    ).read_text()
    assert "crewd:lead -> worker" in tpl
    # No Unicode-arrow attribution and no crewd:-less attribution in examples.
    assert "-> worker]" in tpl
    assert "\u2192 worker]" not in tpl
    assert "[lead \u2192" not in tpl
