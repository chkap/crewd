"""SDK-independent tests for the structured role-handoff channel (#12).

Covers the exactly-one capture invariant, untrusted-payload parsing, and the
transport-authoritative terminal resolution that keeps a role from upgrading a
failed/timed-out/cancelled turn into a completion and refuses to let a
zero/multiple/malformed submission masquerade as a clean completion.
"""
from __future__ import annotations

import threading

from crewd.dispatcher import HandoffOutcome
from crewd.executor import (
    RoleHandoff,
    RoleHandoffCapture,
    parse_role_handoff,
    resolve_role_terminal,
)
from crewd.session_backend import AttemptOutcome, AttemptResult


def _result(outcome: AttemptOutcome, error: str | None = None) -> AttemptResult:
    return AttemptResult(
        attempt_id="",
        session_id="s",
        role="worker",
        outcome=outcome,
        duration=0.01,
        error=error,
        cleanup_confirmed=outcome is not AttemptOutcome.TAINTED,
    )


# ── capture invariant ──
def test_role_capture_single_submission_accepted():
    cap = RoleHandoffCapture()
    payload = {"outcome_class": "completed", "evidence": "PR #42"}
    assert cap.submit(payload) is True
    assert cap.count == 1
    assert cap.result() == payload


def test_role_capture_double_submission_is_invalid_not_last_wins():
    cap = RoleHandoffCapture()
    assert cap.submit({"outcome_class": "completed"}) is True
    assert cap.submit({"outcome_class": "no_progress"}) is False
    assert cap.count == 2
    assert cap.result() is None


def test_role_capture_concurrent_double_submit_resolves_invalid():
    cap = RoleHandoffCapture()
    start = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i):
        start.wait()
        ok = cap.submit({"outcome_class": "completed", "evidence": f"e{i}"})
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1
    assert cap.count == 8
    assert cap.result() is None


# ── parsing (shape only, never trusts semantics) ──
def test_parse_role_handoff_from_dict():
    h = parse_role_handoff(
        {
            "outcome_class": "completed",
            "evidence": "PR #42 green",
            "changed": "added auth module",
            "remaining": "docs",
            "reason": "ready for review",
            "disagreement": "prefer JWT",
        }
    )
    assert h.outcome_class == "completed"
    assert h.evidence == "PR #42 green"
    assert h.changed == "added auth module"
    assert h.remaining == "docs"
    assert h.disagreement == "prefer JWT"


def test_parse_role_handoff_from_json_string_and_missing_fields():
    h = parse_role_handoff('{"outcome_class": "no_progress"}')
    assert h.outcome_class == "no_progress"
    assert h.evidence == "" and h.changed == "" and h.remaining == ""


def test_parse_role_handoff_tolerates_out_of_range_class():
    # Shape validation only — resolution decides it cannot claim a completion.
    h = parse_role_handoff({"outcome_class": "totally-made-up"})
    assert h.outcome_class == "totally-made-up"


# ── transport-authoritative resolution ──
def test_clean_idle_completed_claim_is_honoured():
    h = RoleHandoff(outcome_class="completed", evidence="PR #7", changed="x", remaining="y")
    t = resolve_role_terminal(_result(AttemptOutcome.IDLE_COMPLETED), h, 1)
    assert t.outcome_class is HandoffOutcome.COMPLETED
    assert t.evidence == "PR #7"
    assert t.changed == "x"
    assert t.remaining == "y"


def test_clean_idle_no_progress_claim_is_honoured():
    h = RoleHandoff(outcome_class="no_progress", reason="nothing to do")
    t = resolve_role_terminal(_result(AttemptOutcome.IDLE_COMPLETED), h, 1)
    assert t.outcome_class is HandoffOutcome.NO_PROGRESS
    assert t.outcome_class.is_unproductive


def test_disagreement_is_folded_into_reason_not_authority():
    h = RoleHandoff(outcome_class="completed", reason="done", disagreement="disagree with scope")
    t = resolve_role_terminal(_result(AttemptOutcome.IDLE_COMPLETED), h, 1)
    assert t.outcome_class is HandoffOutcome.COMPLETED
    assert "disagreement: disagree with scope" in t.reason_returned


def test_transport_error_overrides_success_claim():
    # A role that shaped a "completed" handoff but whose turn actually errored
    # must NOT be recorded as completed — the transport wins.
    h = RoleHandoff(outcome_class="completed", evidence="I totally did it")
    t = resolve_role_terminal(_result(AttemptOutcome.SDK_ERROR, error="boom"), h, 1)
    assert t.outcome_class is HandoffOutcome.FAILED
    assert t.remaining == "boom"
    assert t.reason_returned == "sdk:sdk_error"
    # Evidence still carried through for Lead's context.
    assert t.evidence == "I totally did it"


def test_transport_cancel_overrides_claim():
    h = RoleHandoff(outcome_class="completed")
    t = resolve_role_terminal(_result(AttemptOutcome.CANCELLED_CLEAN), h, 1)
    assert t.outcome_class is HandoffOutcome.CANCELLED


def test_transport_taint_overrides_claim():
    h = RoleHandoff(outcome_class="completed")
    t = resolve_role_terminal(_result(AttemptOutcome.TAINTED), h, 1)
    assert t.outcome_class is HandoffOutcome.UNCERTAIN


def test_zero_submission_on_clean_idle_is_protocol_failure():
    t = resolve_role_terminal(_result(AttemptOutcome.IDLE_COMPLETED), None, 0)
    assert t.outcome_class is HandoffOutcome.UNCERTAIN
    assert t.outcome_class.is_unproductive
    assert "role_protocol_failure" in t.reason_returned
    assert "no submit_role_handoff call" in t.reason_returned


def test_multiple_submissions_on_clean_idle_is_protocol_failure():
    # handoff is None because the capture poisons the result on double-submit.
    t = resolve_role_terminal(_result(AttemptOutcome.IDLE_COMPLETED), None, 3)
    assert t.outcome_class is HandoffOutcome.UNCERTAIN
    assert "3 submit_role_handoff calls" in t.reason_returned


def test_invalid_outcome_class_on_clean_idle_is_protocol_failure():
    h = RoleHandoff(outcome_class="made-up")
    t = resolve_role_terminal(_result(AttemptOutcome.IDLE_COMPLETED), h, 1)
    assert t.outcome_class is HandoffOutcome.UNCERTAIN
    assert "invalid outcome_class" in t.reason_returned
