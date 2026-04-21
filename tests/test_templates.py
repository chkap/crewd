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
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
    )
    assert "Lead — demo" in out
    assert "acme/widget" in out
    assert "claude-opus-4.7" in out
    assert "Worker is `gpt-5.4`" in out


def test_render_worker_warns_about_family_difference():
    out = render(
        "agents/worker.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="gpt-5.4",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        worker_family="gpt",
        verifier_family="claude",
    )
    assert "different family (`claude`)" in out
    assert "Never merge your own PR" in out


def test_render_verifier_emphasizes_blackbox():
    out = render(
        "agents/verifier.agent.md.j2",
        workspace_name="demo",
        target_repo=None,
        role_model="claude-opus-4.7",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
        worker_family="gpt",
        verifier_family="claude",
    )
    assert "Black-box" in out
    assert "(repo not yet attached)" in out  # falsy target_repo branch


def test_render_advisory_minimal():
    out = render(
        "agents/advisory.agent.md.j2",
        workspace_name="demo",
        target_repo="acme/widget",
        role_model="gpt-5.2",
        worker_model="gpt-5.4",
        verifier_model="claude-opus-4.7",
        advisory_model="gpt-5.2",
    )
    assert "Cite sources" in out


def test_render_goal_template():
    out = render("GOAL.md.j2", workspace_name="demo")
    assert "GOAL — demo" in out
    assert "Replace this file" in out


def test_render_unknown_template_raises():
    with pytest.raises(Exception):
        render("agents/nope.j2", workspace_name="x")
