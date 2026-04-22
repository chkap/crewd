"""Tests for cfg→agents auto-render (check_and_render)."""
from __future__ import annotations
import os
import time

from crewd.commands import check_and_render, _render_agent_files
from crewd.config import CrewConfig


def test_render_when_agent_missing(tmp_ws):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    # No agent files exist initially
    for role in cfg.roles:
        assert not tmp_ws.agent_file(role).exists()
    rendered = check_and_render(tmp_ws, cfg)
    assert rendered is True
    for role in cfg.roles:
        assert tmp_ws.agent_file(role).exists()


def test_no_render_when_agents_fresh(tmp_ws):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    _render_agent_files(tmp_ws, cfg)
    # Make all agents newer than crew.yaml
    time.sleep(0.01)
    for role in cfg.roles:
        os.utime(tmp_ws.agent_file(role), None)
    rendered = check_and_render(tmp_ws, cfg)
    assert rendered is False


def test_render_when_yaml_newer(tmp_ws):
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    _render_agent_files(tmp_ws, cfg)
    # Bump crew.yaml mtime to be newer
    time.sleep(0.01)
    os.utime(tmp_ws.crew_yaml, None)
    # Force agent files to be older
    yaml_mtime = tmp_ws.crew_yaml.stat().st_mtime
    for role in cfg.roles:
        os.utime(tmp_ws.agent_file(role), (yaml_mtime - 10, yaml_mtime - 10))
    rendered = check_and_render(tmp_ws, cfg)
    assert rendered is True
