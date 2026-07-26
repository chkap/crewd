"""Contract tests for the submit_role_handoff SDK custom tool (#12).

Parallel to test_sdk_leadtool: pins the production role-return channel against
the *actual installed* official SDK symbols (``copilot.define_tool`` /
``tools=[...]``), so a signature drift fails loudly here instead of silently
dropping the only way a dispatched role can hand structured evidence back to
Lead. Also enforces the exactly-one-submission invariant.
"""
from __future__ import annotations

import pathlib

import pytest

from crewd.executor import RoleHandoffCapture
from crewd.sdk_adapter import (
    SdkRoleRuntime,
    make_role_handoff_handler,
    make_role_handoff_tool,
)

sdk = pytest.importorskip("copilot", reason="official SDK not installed")


def test_handler_records_first_candidate():
    cap = RoleHandoffCapture()
    handler = make_role_handoff_handler(cap)
    r1 = handler({"outcome_class": "completed", "evidence": "PR #9"})
    assert r1 == {"accepted": True}
    r2 = handler({"outcome_class": "no_progress"})
    assert r2["accepted"] is False
    assert cap.count == 2
    assert cap.result() is None


def test_make_role_handoff_tool_uses_official_define_tool():
    cap = RoleHandoffCapture()
    tool = make_role_handoff_tool(sdk, cap)
    assert tool.name == "submit_role_handoff"
    assert isinstance(tool.parameters, dict)
    props = tool.parameters.get("properties", {})
    assert "outcome_class" in props
    assert "evidence" in props
    assert "remaining" in props


def test_role_runtime_wires_tool_into_create_session_kwargs():
    cap = RoleHandoffCapture()
    rt = SdkRoleRuntime(
        session_id="s",
        role="worker",
        model="gpt-5.4",
        config_dir=pathlib.Path("/tmp/crewd-rh"),
        working_dir=pathlib.Path("/tmp/crewd-rh"),
        role_handoff_capture=cap,
    )
    kwargs: dict = {}
    rt._add_role_handoff_tool(sdk, kwargs)
    assert "tools" in kwargs
    assert [t.name for t in kwargs["tools"]] == ["submit_role_handoff"]
    assert "custom_tools" not in kwargs


def test_role_runtime_without_capture_registers_no_tool():
    rt = SdkRoleRuntime(
        session_id="s",
        role="worker",
        model="gpt-5.4",
        config_dir=pathlib.Path("/tmp/crewd-rh"),
        working_dir=pathlib.Path("/tmp/crewd-rh"),
    )
    kwargs: dict = {}
    rt._add_role_handoff_tool(sdk, kwargs)
    assert "tools" not in kwargs
