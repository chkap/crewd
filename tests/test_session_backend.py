"""Deterministic tests for the SDK-native role execution boundary.

These exercise the full attempt state machine and lifecycle invariants using a
scripted fake :class:`SdkOps` — no network, no SDK, no Premium calls (the #10
"deterministic fake-SDK tests" acceptance criterion).
"""
from __future__ import annotations

import pytest

from crewd.session_backend import (
    AttemptConfig,
    AttemptOutcome,
    CancelToken,
    LifecyclePhase,
    RunSignal,
    SdkError,
    SessionRegistry,
    TaintedSessionError,
    TaintStore,
    build_session_id,
    redact,
    run_attempt,
)


class FakeOps:
    """Scripted fake of the SdkOps port.

    Configure the outcome of each primitive to drive any state-machine path.
    Records the call order so tests can assert cleanup ownership.
    """

    def __init__(
        self,
        *,
        session_id="sess-1",
        role="worker",
        open_error=False,
        run_signal=RunSignal.IDLE,
        run_error=False,
        abort_confirmed=True,
        abort_error=False,
        disconnect_error=False,
        force_stop_error=False,
        events=None,
    ):
        self.session_id = session_id
        self.role = role
        self._open_error = open_error
        self._run_signal = run_signal
        self._run_error = run_error
        self._abort_confirmed = abort_confirmed
        self._abort_error = abort_error
        self._disconnect_error = disconnect_error
        self._force_stop_error = force_stop_error
        self._events = events or []
        self.calls: list[str] = []

    def open(self, *, resume: bool) -> None:
        self.calls.append(f"open(resume={resume})")
        if self._open_error:
            raise SdkError("open boom")

    def run(self, prompt: str, timeout: float) -> RunSignal:
        self.calls.append("run")
        if self._run_error:
            raise SdkError("run boom")
        return self._run_signal

    def abort(self, timeout: float) -> bool:
        self.calls.append("abort")
        if self._abort_error:
            raise SdkError("abort boom")
        return self._abort_confirmed

    def drain_events(self):
        return list(self._events)

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        if self._disconnect_error:
            raise SdkError("disconnect boom")

    def force_stop(self) -> None:
        self.calls.append("force_stop")
        if self._force_stop_error:
            raise SdkError("force_stop boom")


@pytest.fixture
def taint_store(tmp_path):
    return TaintStore(tmp_path / "tainted.txt")


# ─────────────────────── happy path ───────────────────────
def test_idle_completion_disconnects_and_is_resumable(taint_store):
    ops = FakeOps(run_signal=RunSignal.IDLE)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.IDLE_COMPLETED
    assert res.outcome.resumable and not res.tainted
    assert "disconnect" in ops.calls
    assert "force_stop" not in ops.calls
    assert not taint_store.is_tainted(ops.session_id)


def test_exactly_one_terminal_outcome_and_finished_event(taint_store):
    ops = FakeOps(run_signal=RunSignal.IDLE)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    finished = [e for e in res.events if e.phase is LifecyclePhase.ATTEMPT_FINISHED]
    assert len(finished) == 1
    assert finished[0].detail == AttemptOutcome.IDLE_COMPLETED.value


# ─────────────────────── SDK errors ───────────────────────
def test_open_error_is_sdk_error_no_taint(taint_store):
    ops = FakeOps(open_error=True)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.SDK_ERROR
    assert not res.tainted
    assert res.error and "open boom" in res.error


def test_run_error_disconnects_and_is_sdk_error(taint_store):
    ops = FakeOps(run_error=True)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.SDK_ERROR
    assert "disconnect" in ops.calls
    assert not res.tainted


# ─────────────────────── timeout != cancellation ───────────────────────
def test_wait_timeout_then_confirmed_abort_is_clean_not_success(taint_store):
    ops = FakeOps(run_signal=RunSignal.WAIT_TIMEOUT, abort_confirmed=True)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.ABORTED_CLEAN
    # A timeout that was cleanly aborted is NOT reported as success.
    assert res.outcome is not AttemptOutcome.IDLE_COMPLETED
    assert not res.tainted
    phases = [e.phase for e in res.events]
    assert LifecyclePhase.WAIT_TIMED_OUT in phases
    assert LifecyclePhase.ABORT_REQUESTED in phases
    assert LifecyclePhase.ABORT_CONFIRMED in phases
    assert "disconnect" in ops.calls
    assert "force_stop" not in ops.calls


def test_wait_timeout_unconfirmed_abort_force_stops_and_taints(taint_store):
    ops = FakeOps(run_signal=RunSignal.WAIT_TIMEOUT, abort_confirmed=False)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.TAINTED
    assert res.tainted and not res.outcome.resumable
    assert ops.calls.count("force_stop") == 1
    assert taint_store.is_tainted(ops.session_id)
    phases = [e.phase for e in res.events]
    assert LifecyclePhase.ABORT_FAILED in phases
    assert LifecyclePhase.FORCE_STOPPED in phases
    assert LifecyclePhase.SESSION_TAINTED in phases


def test_abort_raising_also_taints(taint_store):
    ops = FakeOps(run_signal=RunSignal.WAIT_TIMEOUT, abort_error=True)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.TAINTED
    assert taint_store.is_tainted(ops.session_id)


def test_force_stop_error_still_taints(taint_store):
    ops = FakeOps(
        run_signal=RunSignal.WAIT_TIMEOUT, abort_confirmed=False, force_stop_error=True
    )
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.TAINTED
    assert taint_store.is_tainted(ops.session_id)


# ─────────────────────── tainted resume policy ───────────────────────
def test_tainted_session_resume_is_refused(taint_store):
    ops = FakeOps()
    taint_store.taint(ops.session_id)
    with pytest.raises(TaintedSessionError):
        run_attempt(ops, taint_store, prompt="hi", resume=True)
    # It must not have opened the session.
    assert ops.calls == []


def test_tainted_resume_allowed_with_explicit_override(taint_store):
    ops = FakeOps(run_signal=RunSignal.IDLE)
    taint_store.taint(ops.session_id)
    res = run_attempt(
        ops, taint_store, prompt="hi", resume=True, allow_tainted_resume=True
    )
    assert res.outcome is AttemptOutcome.IDLE_COMPLETED
    assert ops.calls[0] == "open(resume=True)"


def test_fresh_create_on_tainted_id_is_allowed(taint_store):
    # A fresh (non-resume) attempt is permitted even if a prior id was tainted;
    # taint only blocks *resume*.
    ops = FakeOps(run_signal=RunSignal.IDLE)
    taint_store.taint(ops.session_id)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.IDLE_COMPLETED


# ─────────────────────── timing is per-attempt, not cumulative ───────────────────────
def test_timing_is_per_attempt_monotonic():
    # Two attempts on a fake monotonic clock report independent durations, not a
    # growing cumulative total (the CLI backend's core measurement defect).
    store = TaintStore.__new__(TaintStore)
    store._cache = set()
    store.path = None  # not persisted in this timing-only test

    class Clock:
        def __init__(self, seq):
            self.seq = list(seq)

        def __call__(self):
            return self.seq.pop(0)

    ops1 = FakeOps(run_signal=RunSignal.IDLE)
    # attempt start=100, ... finish=105 → duration 5
    r1 = run_attempt(ops1, store, prompt="a", resume=False, clock=Clock(range(100, 130)))
    ops2 = FakeOps(run_signal=RunSignal.IDLE)
    r2 = run_attempt(ops2, store, prompt="b", resume=False, clock=Clock(range(1000, 1030)))
    assert r1.duration < 100 and r2.duration < 100  # neither is cumulative
    assert r1.duration == pytest.approx(r2.duration, abs=1.0)


# ─────────────────────── redaction ───────────────────────
def test_redaction_strips_tokens():
    assert "ghp_" not in redact("token ghp_" + "A" * 36)
    assert "«redacted»" in redact("Authorization: Bearer sk-abcdef123456")
    jwt = "eyJhbGciOi.eyJzdWIiOiJ.abc123def456"
    assert jwt not in redact(f"jwt={jwt}")


def test_error_message_is_redacted(taint_store):
    class LeakyOps(FakeOps):
        def open(self, *, resume):
            raise SdkError("failed with token ghp_" + "B" * 36)

    res = run_attempt(LeakyOps(), taint_store, prompt="x", resume=False)
    assert res.outcome is AttemptOutcome.SDK_ERROR
    assert "ghp_" not in (res.error or "")


# ─────────────────────── deterministic session id ───────────────────────
def test_session_id_is_deterministic_and_epoch_scoped():
    a = build_session_id("ws1", "goal:v1", "worker")
    b = build_session_id("ws1", "goal:v1", "worker")
    c = build_session_id("ws1", "goal:v2", "worker")
    d = build_session_id("ws1", "goal:v1", "lead")
    assert a == b            # stable across calls → resumes same session
    assert a != c            # new epoch → new session
    assert a != d            # different role → different session
    assert "worker" in a and "goal-v1" in a
    # Recovery generation changes the id so a tainted session is never reused.
    assert build_session_id("ws1", "goal:v1", "worker", 1) != a


# ─────────────────────── taint store persistence ───────────────────────
def test_taint_store_persists_and_is_idempotent(tmp_path):
    p = tmp_path / "t.txt"
    s1 = TaintStore(p)
    s1.taint("x")
    s1.taint("x")  # idempotent
    s2 = TaintStore(p)  # fresh read → restart cannot lose the taint
    assert s2.is_tainted("x")
    assert p.read_text().count("x") == 1


def test_taint_store_clear_enables_recovery(tmp_path):
    p = tmp_path / "t.txt"
    s = TaintStore(p)
    s.taint("x")
    s.taint("y")
    s.clear("x")
    assert not s.is_tainted("x")
    assert s.is_tainted("y")
    assert TaintStore(p).is_tainted("y")


# ─────────────────────── config passthrough ───────────────────────
def test_attempt_config_defaults():
    c = AttemptConfig()
    assert c.wait_timeout > 0 and c.abort_timeout > 0


# ─────────────────────── unconfirmed cleanup (R4/R5) ───────────────────────
def test_disconnect_failure_taints_even_on_success(taint_store):
    """A successful attempt whose cleanup cannot be confirmed must not remain
    silently resumable — the session id is tainted (resume gate)."""
    ops = FakeOps(run_signal=RunSignal.IDLE, disconnect_error=True)
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert res.outcome is AttemptOutcome.IDLE_COMPLETED  # work did complete
    assert res.cleanup_confirmed is False
    assert res.tainted is True
    assert taint_store.is_tainted(ops.session_id)


def test_drained_events_are_attached_and_redacted(taint_store):
    ops = FakeOps(
        run_signal=RunSignal.IDLE,
        events=["event:assistant_message", "token ghp_" + "A" * 36],
    )
    res = run_attempt(ops, taint_store, prompt="hi", resume=False)
    assert "event:assistant_message" in res.session_event_summaries
    assert all("ghp_" not in s for s in res.session_event_summaries)


# ─────────────────────── adapter correctness (Advisory) ───────────────────────
class _FakeSdkSession:
    """Minimal stand-in for a copilot session to unit-test SdkRoleRuntime.run/abort."""

    def __init__(self, *, wait_raises=None, wait_return=None, events=None):
        self.wait_raises = wait_raises
        self.wait_return = wait_return
        self._events = events or []
        self.sent_prompts = []
        self.aborted = False
        self.abort_count = 0

    async def send_and_wait(self, prompt, timeout=None):
        self.sent_prompts.append(prompt)
        if self.wait_raises is not None:
            raise self.wait_raises
        return self.wait_return

    async def abort(self):
        self.aborted = True
        self.abort_count += 1

    async def get_events(self):
        return list(self._events)


def _runtime_with_session(session):
    from crewd.sdk_adapter import SdkRoleRuntime, _LoopThread

    rt = SdkRoleRuntime(
        session_id="s", role="worker", model="m",
        config_dir=__import__("pathlib").Path("/tmp"),
        working_dir=__import__("pathlib").Path("/tmp"),
    )
    rt._loop = _LoopThread()
    rt._session = session
    return rt


def test_adapter_run_none_return_is_idle_not_timeout():
    """send_and_wait returning None means idle-with-no-assistant-message, NOT a
    wait timeout (which the SDK signals via TimeoutError)."""
    rt = _runtime_with_session(_FakeSdkSession(wait_return=None))
    try:
        assert rt.run("hi", timeout=1) is RunSignal.IDLE
    finally:
        rt._teardown_loop()


def test_adapter_run_timeouterror_is_wait_timeout():
    rt = _runtime_with_session(_FakeSdkSession(wait_raises=TimeoutError()))
    try:
        assert rt.run("hi", timeout=1) is RunSignal.WAIT_TIMEOUT
    finally:
        rt._teardown_loop()


def test_adapter_abort_does_not_send_new_turn():
    """Abort confirmation must not start a new turn; it polls durable events."""
    session = _FakeSdkSession(events=[type("E", (), {"type": "session.abort"})()])
    rt = _runtime_with_session(session)
    try:
        confirmed = rt.abort(timeout=1)
        assert confirmed is True
        assert session.aborted is True
        assert session.sent_prompts == []  # NO new turn sent
    finally:
        rt._teardown_loop()


def test_adapter_abort_unconfirmed_when_no_marker():
    session = _FakeSdkSession(events=[])  # no abort/idle marker
    rt = _runtime_with_session(session)
    try:
        assert rt.abort(timeout=1) is False
        assert session.sent_prompts == []
    finally:
        rt._teardown_loop()


def test_adapter_request_abort_then_abort_issues_once():
    """A non-blocking request_abort (control poll) followed by the state machine's
    confirming abort must issue exactly one SDK abort — single escalation owner."""
    import time

    session = _FakeSdkSession(events=[type("E", (), {"type": "session.abort"})()])
    rt = _runtime_with_session(session)
    try:
        rt.request_abort()          # fire-and-forget schedule on the loop
        time.sleep(0.1)             # let the scheduled coroutine run
        confirmed = rt.abort(timeout=1)  # confirm only; must NOT issue a 2nd abort
        assert confirmed is True
        assert session.abort_count == 1
    finally:
        rt._teardown_loop()


def test_adapter_abort_without_prior_request_issues_once():
    session = _FakeSdkSession(events=[type("E", (), {"type": "session.abort"})()])
    rt = _runtime_with_session(session)
    try:
        assert rt.abort(timeout=1) is True
        assert session.abort_count == 1
    finally:
        rt._teardown_loop()


# ─────────────────────── session registry (epoch + recovery gen) ───────────────────────
def test_registry_first_tick_creates_then_resumes(tmp_path):
    ts = TaintStore(tmp_path / "taint.txt")
    reg = SessionRegistry(tmp_path / "sessions.json", workspace_id="ws1")
    d1 = reg.decide(goal_label="goal:v1", role="worker", taint_store=ts)
    assert d1.resume is False and d1.generation == 0
    # Re-read from disk → same active id, now resumes.
    reg2 = SessionRegistry(tmp_path / "sessions.json", workspace_id="ws1")
    d2 = reg2.decide(goal_label="goal:v1", role="worker", taint_store=ts)
    assert d2.resume is True
    assert d2.session_id == d1.session_id


def test_registry_new_goal_epoch_creates_fresh(tmp_path):
    ts = TaintStore(tmp_path / "taint.txt")
    reg = SessionRegistry(tmp_path / "sessions.json", workspace_id="ws1")
    d1 = reg.decide(goal_label="goal:v1", role="worker", taint_store=ts)
    d2 = reg.decide(goal_label="goal:v2", role="worker", taint_store=ts)
    assert d2.resume is False              # new epoch → fresh session, not resume
    assert d2.session_id != d1.session_id
    assert d2.generation == 0
    # The old epoch still resumes its own session independently.
    d1b = reg.decide(goal_label="goal:v1", role="worker", taint_store=ts)
    assert d1b.resume is True and d1b.session_id == d1.session_id


def test_registry_tainted_active_advances_generation(tmp_path):
    ts = TaintStore(tmp_path / "taint.txt")
    reg = SessionRegistry(tmp_path / "sessions.json", workspace_id="ws1")
    d1 = reg.decide(goal_label="goal:v1", role="worker", taint_store=ts)
    ts.taint(d1.session_id)  # simulate a force-stop taint of the active session
    d2 = reg.decide(goal_label="goal:v1", role="worker", taint_store=ts)
    assert d2.resume is False                 # never resume a tainted id
    assert d2.generation == d1.generation + 1  # advanced recovery generation
    assert d2.session_id != d1.session_id
    assert ts.is_tainted(d1.session_id)       # old taint record PRESERVED for audit


# ─────────────────────── permission handler SDK contract ───────────────────────
def test_permission_handler_allow_all_is_typed_approve():
    """allow-all must be invoked as (request, invocation) and return the SDK's
    typed approve decision — not a {'result': ...} dict (Advisory)."""
    copilot = pytest.importorskip("copilot")
    from copilot.generated.rpc import PermissionDecisionApproveOnce

    from crewd.sdk_adapter import _permission_handler

    handler = _permission_handler(copilot, allow_all=True)
    res = handler(None, {"session_id": "s"})  # two-arg SDK invocation
    assert isinstance(res, PermissionDecisionApproveOnce)


def test_permission_handler_deny_is_typed_user_not_available():
    copilot = pytest.importorskip("copilot")
    from copilot.generated.rpc import PermissionDecisionUserNotAvailable

    from crewd.sdk_adapter import _permission_handler

    handler = _permission_handler(copilot, allow_all=False)
    res = handler(None, {"session_id": "s"})
    assert isinstance(res, PermissionDecisionUserNotAvailable)


# ─────────────────────── external cancellation ───────────────────────
class CancellableOps(FakeOps):
    """FakeOps whose ``run`` blocks until an external abort is requested.

    Models an in-flight turn: ``run`` waits on an event that ``request_abort``
    sets, then returns IDLE (the SDK's send_and_wait returns when the abort
    settles the turn). ``abort`` records how many times it was issued so a test
    can prove the wait-timeout/cancel escalation is a single owner.
    """

    def __init__(self, *, abort_confirmed=True, **kw):
        super().__init__(abort_confirmed=abort_confirmed, **kw)
        import threading

        self._abort_event = threading.Event()
        self.abort_issued = 0
        self.request_abort_calls = 0

    def run(self, prompt: str, timeout: float) -> RunSignal:
        self.calls.append("run")
        # Block until an external cancel unblocks us (bounded so a bug can't hang).
        self._abort_event.wait(timeout=5)
        return RunSignal.IDLE

    def request_abort(self) -> None:
        self.request_abort_calls += 1
        self._abort_event.set()

    def abort(self, timeout: float) -> bool:
        self.abort_issued += 1
        return super().abort(timeout)


def test_external_cancel_confirmed_is_cancelled_clean(taint_store):
    ops = CancellableOps(abort_confirmed=True)
    cancel = CancelToken()

    import threading

    def _cancel_soon():
        # Wait until run() is blocking, then request cancellation.
        while "run" not in ops.calls:
            pass
        cancel.request("operator-stop")

    threading.Thread(target=_cancel_soon, daemon=True).start()
    res = run_attempt(ops, taint_store, prompt="hi", resume=False, cancel=cancel)
    # An idle that arrives because of an external abort is NOT a completion.
    assert res.outcome is AttemptOutcome.CANCELLED_CLEAN
    assert not res.tainted
    assert not taint_store.is_tainted(ops.session_id)
    phases = [e.phase for e in res.events]
    assert LifecyclePhase.CANCEL_REQUESTED in phases
    # Single escalation owner: abort issued exactly once even though request_abort
    # (via the waker) and the confirming abort both ran.
    assert ops.abort_issued == 1


def test_external_cancel_unconfirmed_taints(taint_store):
    ops = CancellableOps(abort_confirmed=False)
    cancel = CancelToken()

    import threading

    def _cancel_soon():
        while "run" not in ops.calls:
            pass
        cancel.request("signal")

    threading.Thread(target=_cancel_soon, daemon=True).start()
    res = run_attempt(ops, taint_store, prompt="hi", resume=False, cancel=cancel)
    # Unconfirmed cancel force-stops and taints (never a clean cancel).
    assert res.outcome is AttemptOutcome.TAINTED
    assert res.tainted
    assert taint_store.is_tainted(ops.session_id)
    assert "force_stop" in ops.calls


def test_cancel_before_wait_still_interrupts(taint_store):
    # Request cancellation BEFORE the attempt starts waiting: bind_waker must fire
    # the pending request immediately so the run doesn't block for the full bound.
    ops = CancellableOps(abort_confirmed=True)
    cancel = CancelToken()
    cancel.request("early")
    res = run_attempt(ops, taint_store, prompt="hi", resume=False, cancel=cancel)
    assert res.outcome is AttemptOutcome.CANCELLED_CLEAN
    assert ops.request_abort_calls >= 1


def test_no_cancel_idle_is_still_completed(taint_store):
    # With a token that is never tripped, a normal idle is an ordinary completion.
    ops = FakeOps(run_signal=RunSignal.IDLE)
    cancel = CancelToken()
    res = run_attempt(ops, taint_store, prompt="hi", resume=False, cancel=cancel)
    assert res.outcome is AttemptOutcome.IDLE_COMPLETED


# ─────────────────────── CancelToken unit ───────────────────────
def test_cancel_token_first_reason_wins_idempotent():
    tok = CancelToken()
    fired = []
    tok.bind_waker(lambda: fired.append(1))
    tok.request("first")
    tok.request("second")
    assert tok.is_requested
    assert tok.reason == "first"
    # Waker fires once per first request (subsequent requests are no-ops).
    assert fired == [1]


def test_cancel_token_waker_fires_if_bound_after_request():
    tok = CancelToken()
    tok.request("pre-bind")
    fired = []
    tok.bind_waker(lambda: fired.append(1))
    assert fired == [1]


def test_cancel_token_waker_exception_is_swallowed():
    tok = CancelToken()

    def _boom():
        raise RuntimeError("waker boom")

    tok.bind_waker(_boom)
    # Must not raise into the requester (a control poll / signal path).
    tok.request("x")
    assert tok.is_requested


def test_cancel_token_concurrent_requests_single_owner():
    tok = CancelToken()
    fired = []
    tok.bind_waker(lambda: fired.append(1))

    import threading

    barrier = threading.Barrier(8)

    def _req(n):
        barrier.wait()
        tok.request(f"r{n}")

    threads = [threading.Thread(target=_req, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly one reason won and the waker fired exactly once despite 8 racers.
    assert tok.is_requested
    assert fired == [1]
    assert tok.reason is not None
