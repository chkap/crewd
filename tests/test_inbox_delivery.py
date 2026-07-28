"""Host-owned operator inbox delivery (goal:v2 task #29).

The orchestrator (host) — not the model — must read pending inbox content before
constructing a role/Lead prompt, attach it as a bounded, delimited, redacted
payload with priority + arrival ordering preserved, and archive it only after the
attempt terminalizes. The first test is the reproduction of #29's "does not
consume operator inbox" defect: before the fix the host neither injects nor
archives the message.
"""
from __future__ import annotations

from pathlib import Path

from crewd.config import CrewConfig, GoalState
from crewd.orchestrator import Orchestrator
from crewd.workspace import Workspace

from fakes import FakeExecutor, dispatch_to, finish


def _orch(tmp_ws: Workspace, fake: FakeExecutor) -> Orchestrator:
    cfg = CrewConfig.load(tmp_ws.crew_yaml)
    cfg.loop.max_cycles = 0
    gs = GoalState(version=1, label="goal:v1", cycles=0, goal_md_sha256="x")
    gs.save(tmp_ws.goal_json)
    return Orchestrator(tmp_ws, cfg, fake, gs, max_steps=200)


def _inbox(ws: Workspace, role: str) -> Path:
    p = ws.state_dir / "inbox"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{role}.md"


def test_host_injects_and_archives_worker_inbox(tmp_ws: Workspace):
    """Reproduction + fix: host delivers the worker inbox into the dispatched
    prompt and archives the file after the attempt terminalizes."""
    _inbox(tmp_ws, "worker").write_text("[OVERRIDE] 2026-01-01T00:00:00+00:00 halt and re-plan\n")

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    _orch(tmp_ws, fake).run(once=False)

    # Host injected the operator message into the worker's dispatched prompt.
    assert fake.role_calls, "worker was never dispatched"
    worker_prompt = fake.role_calls[0].prompt
    assert "halt and re-plan" in worker_prompt
    assert "OPERATOR INBOX" in worker_prompt

    # The live inbox file was consumed by the host (not left for the model).
    assert not _inbox(tmp_ws, "worker").exists()
    # ...and preserved as an audit trail, not destroyed.
    processed = list((tmp_ws.state_dir / "inbox").glob("worker.processed.*"))
    assert processed, "consumed inbox must be archived, not deleted"


# ── unit: InboxService lifecycle & rendering ──
from crewd.inbox import (  # noqa: E402
    InboxService,
    parse_messages,
    redact_secrets,
    render_payload,
)


def _svc(tmp_path: Path) -> InboxService:
    d = tmp_path / "inbox"
    d.mkdir()
    return InboxService(d)


def test_priority_and_arrival_ordering():
    raw = (
        "[INFO] t1 first-info\n"
        "[OVERRIDE] t2 stop-now\n"
        "[ADVICE] t3 consider-x\n"
        "[INFO] t4 second-info\n"
        "[OVERRIDE] t5 also-stop\n"
    )
    payload = render_payload(parse_messages(raw))
    assert payload is not None
    order = [payload.index(s) for s in
             ("stop-now", "also-stop", "consider-x", "first-info", "second-info")]
    assert order == sorted(order), f"not ordered by priority then arrival: {order}"


def test_parses_block_and_line_forms_interleaved():
    raw = (
        "[OVERRIDE] 2026 line-override\n"
        "\n---\n## [operator @ 2026-02-02]\nblock body line one\nline two\n"
    )
    msgs = parse_messages(raw)
    assert [m.priority for m in msgs] == ["OVERRIDE", "INFO"]
    assert msgs[0].body == "line-override"
    assert "block body line one" in msgs[1].body and "line two" in msgs[1].body


def test_new_goal_override_block_is_high_priority():
    raw = "\n---\n## [OVERRIDE @ 2026-03-03]\nnew goal epoch v2\n"
    msgs = parse_messages(raw)
    assert msgs[0].priority == "OVERRIDE"
    assert "new goal epoch v2" in render_payload(msgs)


def test_redaction_of_secrets():
    txt = "use token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and password=hunter2"
    red = redact_secrets(txt)
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in red
    assert "hunter2" not in red
    assert "[REDACTED]" in red


def test_render_bounds_payload_size():
    raw = "[INFO] t " + ("x" * 20000) + "\n"
    payload = render_payload(parse_messages(raw))
    assert payload is not None
    from crewd.inbox import MAX_PAYLOAD_CHARS
    assert len(payload) <= MAX_PAYLOAD_CHARS


def test_empty_or_whitespace_inbox_delivers_nothing(tmp_path: Path):
    svc = _svc(tmp_path)
    (svc._live("worker")).write_text("   \n\n")  # type: ignore[attr-defined]
    assert svc.deliver("worker", "a1") is None
    assert svc.counts("worker") == {"pending": 0, "delivering": 0, "processed": 0}


def test_deliver_stages_then_acknowledge_archives(tmp_path: Path):
    svc = _svc(tmp_path)
    live = svc._live("worker")  # type: ignore[attr-defined]
    live.write_text("[OVERRIDE] t stop\n")

    payload = svc.deliver("worker", "att-1")
    assert payload and "stop" in payload
    # Staged (attached-but-unacked): live consumed, not yet processed.
    assert not live.exists()
    assert svc.counts("worker")["delivering"] == 1
    assert svc.counts("worker")["processed"] == 0

    svc.acknowledge("worker", "att-1")
    assert svc.counts("worker")["delivering"] == 0
    assert svc.counts("worker")["processed"] == 1


def test_deliver_is_idempotent_for_same_attempt(tmp_path: Path):
    """A retry of the same attempt re-delivers identical content (crash-after
    attach, before ack)."""
    svc = _svc(tmp_path)
    svc._live("worker").write_text("[OVERRIDE] t stop\n")  # type: ignore[attr-defined]
    first = svc.deliver("worker", "att-1")
    second = svc.deliver("worker", "att-1")
    assert first == second


def test_crash_before_ack_refolds_into_next_same_role_attempt(tmp_path: Path):
    """A message staged for a crashed attempt is retained and re-delivered to the
    next attempt of the SAME role — never lost, never cross-role."""
    svc = _svc(tmp_path)
    svc._live("worker").write_text("[OVERRIDE] t stop\n")  # type: ignore[attr-defined]
    svc.deliver("worker", "crashed")  # attempt crashes before acknowledge()

    # A brand new attempt still receives the message.
    payload = svc.deliver("worker", "fresh")
    assert payload and "stop" in payload
    # The orphaned staging was absorbed into the fresh attempt (only one staging).
    assert svc.counts("worker")["delivering"] == 1


def test_message_never_consumed_by_wrong_role(tmp_path: Path):
    svc = _svc(tmp_path)
    svc._live("worker").write_text("[OVERRIDE] t worker-only\n")  # type: ignore[attr-defined]
    assert svc.deliver("verifier", "v1") is None
    assert svc.deliver("worker", "w1") is not None


def test_orchestrator_retains_inbox_across_crash_then_delivers(tmp_ws: Workspace):
    """Integration: if an attempt is dispatched but never acknowledged (crash),
    the operator message is redelivered on the next dispatch of that role."""
    _inbox(tmp_ws, "worker").write_text("[OVERRIDE] t re-plan-now\n")
    svc = InboxService.for_workspace(tmp_ws)
    # Simulate an attempt that attached the inbox but crashed before ack.
    assert svc.deliver("worker", "att-crashed") is not None

    fake = FakeExecutor(lead_script=[dispatch_to("worker"), finish("done")])
    _orch(tmp_ws, fake).run(once=False)

    assert "re-plan-now" in fake.role_calls[0].prompt
    assert not _inbox(tmp_ws, "worker").exists()
    assert list((tmp_ws.state_dir / "inbox").glob("worker.processed.*"))

