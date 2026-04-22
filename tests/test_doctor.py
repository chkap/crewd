"""Tests for cmd_doctor."""
from __future__ import annotations
from pathlib import Path

from crewd.commands import cmd_doctor


def test_doctor_runs_on_minimal_workspace(tmp_ws, capsys):
    """On a fresh workspace with no agent files, doctor should run and exit non-zero (missing agent.md)."""
    rc = cmd_doctor(tmp_ws.root)
    # tmp_ws has no agent.md files rendered → ERROR severity → exit 1
    assert rc == 1
    out = capsys.readouterr().out
    assert "crewd doctor" in out
    assert "roles" in out


def test_doctor_zero_when_clean(tmp_ws, capsys):
    """With agent files rendered and a real GOAL.md, doctor should return 0."""
    from crewd.commands import _render_agent_files
    from crewd.config import CrewConfig

    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    _render_agent_files(tmp_ws, cfg)
    # Make agent files newer than crew.yaml
    import os, time
    time.sleep(0.01)
    for role in cfg.roles:
        os.utime(tmp_ws.agent_file(role), None)
    # Clean GOAL.md (no template placeholder)
    tmp_ws.goal_md.write_text("# real goal\n")
    rc = cmd_doctor(tmp_ws.root)
    assert rc == 0


def test_doctor_no_workspace(tmp_path: Path):
    """No crew.yaml → exit 1."""
    rc = cmd_doctor(tmp_path)
    assert rc == 1


def test_doctor_stuck_state_flagged(tmp_ws, capsys):
    """STOPPED present + cycle 0 → flagged as ERROR."""
    from crewd.commands import _render_agent_files
    from crewd.config import CrewConfig

    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    _render_agent_files(tmp_ws, cfg)
    tmp_ws.goal_md.write_text("# real goal\n")
    tmp_ws.stop("test")
    rc = cmd_doctor(tmp_ws.root)
    assert rc == 1
    out = capsys.readouterr().out
    assert "stuck" in out.lower() or "STOPPED" in out
