"""Shared pytest fixtures."""
from __future__ import annotations
from pathlib import Path
import pytest

from crewd.workspace import Workspace
from crewd.config import default_config


@pytest.fixture
def tmp_ws(tmp_path: Path) -> Workspace:
    """Initialized workspace at tmp_path/ws with target attached."""
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    ws = Workspace(ws_root)
    ws.ensure_skeleton()
    cfg = default_config(name="testcrew", repo="acme/widget")
    cfg.save(ws.crew_yaml)
    ws.goal_md.write_text("# GOAL\n\nDo the thing.\n")
    # Fake checkout so doctor / run don't bail
    co = ws.checkout_dir(cfg.target.checkout)
    co.mkdir(parents=True, exist_ok=True)
    return ws
