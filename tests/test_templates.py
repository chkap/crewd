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
