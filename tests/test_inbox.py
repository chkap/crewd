"""Tests for cmd_inbox_append."""
from __future__ import annotations
from pathlib import Path

from crewd import commands
from crewd.workspace import Workspace


def test_cmd_inbox_append_creates_file_with_prefix(tmp_ws: Workspace):
    rc = commands.cmd_inbox_append(tmp_ws.root, "worker", "OVERRIDE", "stop and reset")
    assert rc == 0
    inbox = tmp_ws.state_dir / "inbox" / "worker.md"
    assert inbox.exists()
    body = inbox.read_text()
    assert body.startswith("[OVERRIDE] ")
    assert "stop and reset" in body
    assert body.endswith("\n")


def test_cmd_inbox_append_appends_multiple_lines(tmp_ws: Workspace):
    assert commands.cmd_inbox_append(tmp_ws.root, "lead", "ADVICE", "first") == 0
    assert commands.cmd_inbox_append(tmp_ws.root, "lead", "INFO", "second") == 0
    body = (tmp_ws.state_dir / "inbox" / "lead.md").read_text()
    lines = [ln for ln in body.splitlines() if ln]
    assert len(lines) == 2
    assert lines[0].startswith("[ADVICE] ") and "first" in lines[0]
    assert lines[1].startswith("[INFO] ") and "second" in lines[1]


def test_cmd_inbox_append_priority_case_insensitive(tmp_ws: Workspace):
    rc = commands.cmd_inbox_append(tmp_ws.root, "verifier", "advice", "lower")
    assert rc == 0
    body = (tmp_ws.state_dir / "inbox" / "verifier.md").read_text()
    assert body.startswith("[ADVICE] ")


def test_cmd_inbox_append_rejects_unknown_role(tmp_ws: Workspace):
    rc = commands.cmd_inbox_append(tmp_ws.root, "captain", "INFO", "hi")
    assert rc == 1


def test_cmd_inbox_append_rejects_unknown_priority(tmp_ws: Workspace):
    rc = commands.cmd_inbox_append(tmp_ws.root, "worker", "URGENT", "hi")
    assert rc == 1


def test_cmd_inbox_append_rejects_uninitialized_workspace(tmp_path: Path):
    rc = commands.cmd_inbox_append(tmp_path / "nope", "worker", "INFO", "hi")
    assert rc == 1
