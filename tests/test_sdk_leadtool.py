"""Contract tests for the submit_lead_decision SDK custom tool.

These pin the production Lead-decision channel against the *actual installed*
official SDK symbols (``copilot.define_tool`` / ``tools=[...]``), so a signature
drift like the removed ``CustomTool`` fails loudly here instead of silently
dropping the only way Lead can deliver a routing decision. They also enforce the
accepted #17 invariant that a Lead turn must submit *exactly one* decision:
zero or multiple submissions are invalid.
"""
from __future__ import annotations

import threading

import pytest

from crewd.executor import LeadDecisionCapture
from crewd.sdk_adapter import (
    SdkRoleRuntime,
    make_lead_decision_handler,
    make_lead_decision_tool,
    sdk_available,
)

sdk = pytest.importorskip("copilot", reason="official SDK not installed")


# ── capture invariant (SDK-independent) ──
def test_capture_single_submission_accepted():
    cap = LeadDecisionCapture()
    assert cap.submit({"kind": "finish", "final_acceptance": "ok"}) is True
    assert cap.count == 1
    assert cap.result() == {"kind": "finish", "final_acceptance": "ok"}


def test_capture_double_submission_is_invalid_not_last_wins():
    cap = LeadDecisionCapture()
    assert cap.submit({"kind": "dispatch", "role": "worker"}) is True
    # A second submission is rejected AND poisons the result (not last-wins).
    assert cap.submit({"kind": "finish", "final_acceptance": "sneaky"}) is False
    assert cap.count == 2
    assert cap.result() is None


def test_capture_zero_submission_is_invalid():
    cap = LeadDecisionCapture()
    assert cap.count == 0
    assert cap.result() is None


def test_capture_concurrent_double_submit_resolves_invalid():
    cap = LeadDecisionCapture()
    start = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i):
        start.wait()
        ok = cap.submit({"kind": "dispatch", "role": f"r{i}"})
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly one submission was accepted; the turn is nonetheless invalid.
    assert sum(1 for r in results if r) == 1
    assert cap.count == 8
    assert cap.result() is None


# ── handler records into the capture ──
def test_handler_records_first_candidate():
    cap = LeadDecisionCapture()
    handler = make_lead_decision_handler(cap)
    r1 = handler({"kind": "dispatch", "role": "worker", "ack_handoff_ids": []})
    assert r1 == {"accepted": True}
    r2 = handler({"kind": "finish", "final_acceptance": "x"})
    assert r2["accepted"] is False
    assert cap.count == 2
    assert cap.result() is None


# ── official SDK API construction (fails loudly on signature drift) ──
def test_make_lead_decision_tool_uses_official_define_tool():
    cap = LeadDecisionCapture()
    tool = make_lead_decision_tool(sdk, cap)
    assert tool.name == "submit_lead_decision"
    # Built via the documented define_tool surface → a real Tool with a schema.
    assert isinstance(tool.parameters, dict)
    props = tool.parameters.get("properties", {})
    assert "kind" in props
    assert "ack_handoff_ids" in props


def test_lead_runtime_wires_tool_into_create_session_kwargs():
    cap = LeadDecisionCapture()
    rt = SdkRoleRuntime(
        session_id="s",
        role="lead",
        model="claude-sonnet-4.6",
        config_dir=__import__("pathlib").Path("/tmp/crewd-x"),
        working_dir=__import__("pathlib").Path("/tmp/crewd-x"),
        lead_decision_capture=cap,
    )
    kwargs: dict = {}
    rt._add_lead_decision_tool(sdk, kwargs)
    assert "tools" in kwargs
    assert [t.name for t in kwargs["tools"]] == ["submit_lead_decision"]
    # The retired CustomTool/custom_tools shape must NOT be used.
    assert "custom_tools" not in kwargs
