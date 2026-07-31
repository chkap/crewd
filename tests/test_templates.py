"""Tests for crewd.templates_render — Jinja rendering of agent files + GOAL."""
from __future__ import annotations
import pytest

from crewd.templates_render import render


def test_render_lead_agent_includes_workspace_and_repo():
    out = render(
        "agents/lead.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="claude-opus-4.7",
        role_name="lead",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=True,
    )
    assert "Lead — demo" in out
    assert "acme/widget" in out
    assert "claude-opus-4.7" in out
    assert "Worker is `gpt-5.4`" in out
    assert "cfg/lead/" in out  # workspace layout included
    assert "decision log" in out
    assert "genuinely necessary" in out
    assert "human-facing anchor" in out
    assert "single umbrella GOAL issue" in out
    assert "plain-language summary of the user goal" in out
    assert "Close the GOAL issue" in out
    assert "state/PAUSED" in out
    assert "human-blocked:" in out
    # Dispatcher model: Lead routes via a single typed decision, not files/ticks.
    assert "submit_lead_decision" in out
    assert "Lead-directed dispatcher" in out
    assert "finish" in out and "final_acceptance" in out
    assert "operator" in out  # operator stop distinct from finish


def test_render_worker_warns_about_family_difference():
    out = render(
        "agents/worker.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="gpt-5.4",
        role_name="worker",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=True,
        worker_family="gpt",
        verifier_family="claude",
    )
    assert "different family (`claude`)" in out
    assert "Never merge your own PR" in out
    assert "cfg/worker/" in out
    assert "implementation note" in out
    assert "truly blocked" in out


def test_render_worker_without_advisory_omits_advisory_refs():
    out = render(
        "agents/worker.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="gpt-5.4",
        role_name="worker",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=False,
        worker_family="gpt",
        verifier_family="claude",
    )
    assert "Advisory" not in out
    assert "You own the technical approach" in out
    assert "Gather all context first" in out


def test_render_verifier_emphasizes_blackbox():
    out = render(
        "agents/verifier.agent.md.j2",
        workspace_name="demo",
        target_repo=None,
        role_model="claude-opus-4.7",
        role_name="verifier",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=True,
        worker_family="gpt",
        verifier_family="claude",
    )
    assert "Black-box" in out
    assert "(repo not yet attached)" in out  # falsy target_repo branch
    assert "spec attack" in out
    assert "regression-suspicious" in out
    assert "derive your own manual checks from the linked issue" in out
    # bug-hunting + local env + in-development scope + harder final gate
    assert "hunt the bugs a real user will hit" in out
    assert ".venv" in out
    assert "under active development" in out
    assert "200% harder" in out


def test_render_verifier_without_advisory_omits_advisory():
    out = render(
        "agents/verifier.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="claude-opus-4.7",
        role_name="verifier",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=False,
        worker_family="gpt",
        verifier_family="claude",
    )
    assert "Advisory" not in out
    assert "Close the loop" in out  # renumbered responsibilities still present


def test_render_advisory_minimal():
    out = render(
        "agents/advisory.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="gpt-5.2",
        role_name="advisory",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
    )
    assert "Cite sources" in out
    assert "research scientist / strategic advisor" in out
    assert "non-binding" in out
    assert "Options" in out
    assert "truly necessary" in out


def test_render_other_roles_reference_non_binding_advisory():
    lead = render(
        "agents/lead.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="claude-opus-4.7",
        role_name="lead",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=True,
    )
    worker = render(
        "agents/worker.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="gpt-5.4",
        role_name="worker",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=True,
        worker_family="gpt",
        verifier_family="claude",
    )
    verifier = render(
        "agents/verifier.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="claude-opus-4.7",
        role_name="verifier",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=True,
        worker_family="gpt",
        verifier_family="claude",
    )
    assert "Advisory guidance is non-binding" in lead
    assert "consider them seriously, but use your own judgment" in worker
    assert "Their guidance is non-binding" in verifier


def test_render_lead_omits_advisory_when_disabled():
    out = render(
        "agents/lead.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="claude-opus-4.7",
        role_name="lead",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        advisory_enabled=False,
    )
    assert "Use Advisory strategically" not in out
    assert "Advisory is `gpt-5.2`" not in out
    assert "Coordinate Worker and Verifier" in out
    # Lead should not impose a fixed issue-count quota
    assert "3–6 issues" not in out
    assert "usually at least 2" in out


def test_render_goal_template():
    out = render("GOAL.md.j2", workspace_name="demo")
    assert "GOAL — demo" in out
    assert "Replace this file" in out


def test_render_unknown_template_raises():
    with pytest.raises(Exception):
        render("agents/nope.j2", workspace_name="x")


# ── #12: dispatcher-model + structured-handoff contract in every role prompt ──
_BASE_CTX = dict(
    workspace_name="demo",
    target_repo="acme/widget",
    worker_model="gpt-5.4",
    verifier_model="claude-opus-4.7",
    advisory_model="gpt-5.2",
    advisory_enabled=True,
    worker_family="gpt",
    verifier_family="claude",
)


@pytest.mark.parametrize("role", ["worker", "verifier", "advisory"])
def test_non_lead_roles_carry_dispatch_model_and_handoff_contract(role):
    out = render(
        f"agents/{role}.agent.md.j2", role_name=role, role_model="m", **_BASE_CTX
    )
    # No fixed-cycle / round-robin language; dispatched one attempt at a time.
    assert "Lead-directed dispatcher" in out
    assert "one dispatched attempt" in out
    assert "routing control returns to Lead" in out
    assert "Do one tick and stop" not in out
    # Structured handoff channel with the real outcome names + fields.
    assert "submit_role_handoff" in out
    assert "outcome_class" in out
    assert "completed" in out and "no_progress" in out
    assert "evidence" in out and "remaining" in out and "disagreement" in out
    # Transport authority is stated so a role can't upgrade a failed turn.
    assert "transport lifecycle is authoritative" in out
    # Routing stays with Lead only.
    assert "only Lead routes" in out


def test_lead_prompt_describes_all_decision_kinds_and_operator_distinction():
    out = render("agents/lead.agent.md.j2", role_name="lead", role_model="m", **_BASE_CTX)
    for kind in ("dispatch", "wait", "pause", "finish"):
        assert kind in out
    # continue_lead was removed from the Lead decision contract (#65): the model
    # must not be offered a self-loop routing outcome.
    assert "continue_lead" not in out
    assert "submit_lead_decision" in out
    assert "wake_condition" in out
    assert "human_blocker" in out
    assert "final_acceptance" in out
    # Structured handoffs feed Lead's routing.
    assert "structured handoff" in out
    # Operator stop is distinct from Lead's finish/pause.
    assert "operator" in out
    assert "crewd stop" in out
    # Lead no longer hand-writes sentinel files to pause/finish.
    assert "you do not write" in out


def test_no_role_prompt_uses_legacy_tick_loop_language():
    for role in ("lead", "worker", "verifier", "advisory"):
        out = render(
            f"agents/{role}.agent.md.j2", role_name=role, role_model="m", **_BASE_CTX
        )
        assert "Do one tick and stop" not in out
        assert "round-robin" not in out or "not a fixed round-robin" in out
