"""SDK-native role execution boundary.

This module defines the *domain port* for running one role attempt against a
persistent Copilot session, independent of the concrete transport (the official
`github-copilot-sdk`) and independent of the orchestration/routing layer (#11).

Design (derived from the #9 retrospective requirements R4–R6 and the Advisory
capability review on #10):

* An **attempt** is modelled as an explicit state machine, not a thin wrapper
  around ``send_and_wait``. A wait-timeout is *not* cancellation: after a wait
  timeout we must issue ``abort()`` under its own bound and require confirmation
  before declaring a clean cancellation (R5).
* Forced termination **taints** the session id; a tainted session must never be
  resumed until an operator/Lead explicitly chooses unsafe-resume or
  fresh-session recovery (R4).
* Every attempt yields **exactly one** terminal :class:`AttemptOutcome` and
  adapter-owned lifecycle events carrying attempt identity and *per-attempt*
  (non-cumulative) monotonic timing (R6). The old CLI backend could only report
  cumulative per-session footers; this boundary fixes that.

The SDK is kept behind the narrow :class:`SdkOps` port so the whole state
machine is exercised by a deterministic fake with no network or Premium calls.
The concrete SDK implementation lives in :mod:`crewd.sdk_adapter`.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


# ─────────────────────────── outcomes & signals ───────────────────────────
class AttemptOutcome(str, Enum):
    """The single terminal classification of a role attempt.

    Distinct outcomes are mandatory (R5): a timeout, a clean post-timeout
    cancellation, an SDK error, and a forced-kill taint must never collapse to a
    single "returned" state the way the CLI backend's exit codes did.
    """

    IDLE_COMPLETED = "idle_completed"      # session reached idle within the wait bound
    SDK_ERROR = "sdk_error"                # SDK raised; session left connected, resumable
    ABORTED_CLEAN = "aborted_clean"        # wait timed out, abort() confirmed idle
    TAINTED = "tainted"                    # abort unconfirmed → force_stop → unsafe to resume

    @property
    def is_terminal_success(self) -> bool:
        return self is AttemptOutcome.IDLE_COMPLETED

    @property
    def taints_session(self) -> bool:
        return self is AttemptOutcome.TAINTED

    @property
    def resumable(self) -> bool:
        """Whether the same session id may be resumed on a later attempt."""
        return self is not AttemptOutcome.TAINTED


class RunSignal(str, Enum):
    """Result of a single ``run`` (send_and_wait) primitive on :class:`SdkOps`."""

    IDLE = "idle"                # session.idle observed before the wait timeout
    WAIT_TIMEOUT = "wait_timeout"  # wait elapsed; in-flight work NOT aborted yet


class LifecyclePhase(str, Enum):
    """Adapter-owned lifecycle events (not SDK session events).

    These record host-level transitions the SDK history does not capture.
    """

    ATTEMPT_STARTED = "attempt_started"
    SESSION_OPENED = "session_opened"
    WAIT_TIMED_OUT = "wait_timed_out"
    ABORT_REQUESTED = "abort_requested"
    ABORT_CONFIRMED = "abort_confirmed"
    ABORT_FAILED = "abort_failed"
    FORCE_STOPPED = "force_stopped"
    SESSION_TAINTED = "session_tainted"
    DISCONNECTED = "disconnected"
    ATTEMPT_FINISHED = "attempt_finished"


class TaintedSessionError(RuntimeError):
    """Raised when resuming a session id that was previously tainted."""


class SdkError(RuntimeError):
    """Adapter-normalised error raised by an :class:`SdkOps` primitive."""


# ─────────────────────────── event records ───────────────────────────
_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),          # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),  # JWT
    # `Bearer <token>` (space-separated auth scheme).
    re.compile(r"(?i)\bbearer\s+\S+"),
    # `key: value` / `key=value` style secrets.
    re.compile(r"(?i)\b(authorization|token|secret|password|api[_-]?key)\b\s*[:=]\s*\S+"),
]


def redact(text: str) -> str:
    """Strip obvious secrets before anything reaches a durable adapter log.

    The adapter never writes raw, unrestricted SDK payloads to durable logs; it
    records structured lifecycle metadata plus redacted summaries only.
    """
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("«redacted»", out)
    return out


@dataclass(frozen=True)
class LifecycleEvent:
    """One adapter-owned lifecycle event with attempt identity + monotonic time."""

    attempt_id: str
    session_id: str
    role: str
    phase: LifecyclePhase
    # Seconds since the attempt started (monotonic, per-attempt — never cumulative).
    elapsed: float
    detail: str = ""

    def to_line(self) -> str:
        d = f" {redact(self.detail)}" if self.detail else ""
        return (
            f"[{self.elapsed:8.3f}s] {self.role} {self.session_id} "
            f"{self.attempt_id} {self.phase.value}{d}"
        )


@dataclass
class AttemptResult:
    """Terminal result of one attempt: exactly one outcome + its lifecycle trail."""

    attempt_id: str
    session_id: str
    role: str
    outcome: AttemptOutcome
    duration: float                        # per-attempt wall time (monotonic)
    events: list[LifecycleEvent] = field(default_factory=list)
    error: str | None = None               # redacted SDK error message, if any
    tainted: bool = False

    def __post_init__(self) -> None:
        # Invariant: exactly one terminal outcome, and taint iff TAINTED.
        self.tainted = self.outcome.taints_session


# ─────────────────────────── SDK port (fakeable) ───────────────────────────
@runtime_checkable
class SdkOps(Protocol):
    """Narrow, fakeable port over one Copilot session's low-level operations.

    Deliberately does NOT expose the SDK client/JSON-RPC. The concrete
    implementation (real or fake) owns the transport; the state machine below
    owns policy. All methods are synchronous from the caller's perspective — the
    real adapter bridges the SDK's asyncio API internally.
    """

    session_id: str
    role: str

    def open(self, *, resume: bool) -> None:
        """Create (resume=False) or resume (resume=True) the session.

        Must raise :class:`SdkError` on failure.
        """
        ...

    def run(self, prompt: str, timeout: float) -> RunSignal:
        """Send ``prompt`` and wait up to ``timeout`` for the session to idle.

        Returns :data:`RunSignal.IDLE` if idle was reached, or
        :data:`RunSignal.WAIT_TIMEOUT` if the wait elapsed while work is still
        in flight (which is NOT a cancellation). Raises :class:`SdkError` on an
        SDK-level failure.
        """
        ...

    def abort(self, timeout: float) -> bool:
        """Request abort and wait up to ``timeout`` for confirmed idle.

        Returns True iff the session confirmed idle (clean cancellation). Returns
        False or raises :class:`SdkError` if the abort could not be confirmed.
        """
        ...

    def drain_events(self) -> list[str]:
        """Return durable SDK session-event summaries produced this attempt.

        Summaries only — never raw unrestricted payloads. Used for the durable
        history; redaction is applied by the caller before persistence.
        """
        ...

    def disconnect(self) -> None:
        """Release handlers/resources; preserve on-disk session state (resumable)."""
        ...

    def force_stop(self) -> None:
        """Exceptional: kill the owned runtime. The session becomes untrusted."""
        ...


# ─────────────────────────── taint store ───────────────────────────
class TaintStore:
    """Durable record of session ids that were force-stopped (unsafe to resume).

    Persisted as one session id per line so a restart cannot lose the fact that a
    session was tainted (R3/R4). Idempotent.
    """

    def __init__(self, path: Path):
        self.path = path
        self._cache: set[str] | None = None

    def _load(self) -> set[str]:
        if self._cache is None:
            if self.path.exists():
                self._cache = {
                    ln.strip()
                    for ln in self.path.read_text().splitlines()
                    if ln.strip()
                }
            else:
                self._cache = set()
        return self._cache

    def is_tainted(self, session_id: str) -> bool:
        return session_id in self._load()

    def taint(self, session_id: str) -> None:
        cache = self._load()
        if session_id in cache:
            return
        cache.add(session_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(session_id + "\n")

    def clear(self, session_id: str) -> None:
        """Operator/Lead-driven recovery: clear taint to allow explicit resume."""
        cache = self._load()
        if session_id not in cache:
            return
        cache.discard(session_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(s + "\n" for s in sorted(cache)))


# ─────────────────────────── deterministic session id ───────────────────────────
def build_session_id(workspace_id: str, goal_label: str, role: str) -> str:
    """Deterministic, collision-resistant session id.

    Derived from workspace identity + goal epoch + role (Advisory: never merely
    role), so the same role in the same goal epoch resumes the same session, and
    a new goal epoch starts a fresh one.
    """
    raw = f"{workspace_id}\x1f{goal_label}\x1f{role}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"crewd-{role}-{goal_label.replace(':', '-')}-{digest}"


# ─────────────────────────── the attempt state machine ───────────────────────────
@dataclass
class AttemptConfig:
    wait_timeout: float = 900.0     # send_and_wait bound (per-tick budget)
    abort_timeout: float = 30.0     # bound for post-timeout abort confirmation


def run_attempt(
    ops: SdkOps,
    taint_store: TaintStore,
    *,
    prompt: str,
    resume: bool,
    config: AttemptConfig | None = None,
    allow_tainted_resume: bool = False,
    clock=time.monotonic,
) -> AttemptResult:
    """Drive one role attempt through the lifecycle state machine.

    Guarantees:
      * exactly one terminal :class:`AttemptOutcome`;
      * a wait-timeout is never reported as a successful cancellation;
      * an unconfirmed abort force-stops and taints the session id;
      * a tainted session id is refused for resume unless explicitly allowed;
      * timing is per-attempt (monotonic), never cumulative.
    """
    cfg = config or AttemptConfig()
    session_id = ops.session_id
    role = ops.role
    attempt_id = _new_attempt_id(session_id, clock)
    start = clock()
    events: list[LifecycleEvent] = []

    def emit(phase: LifecyclePhase, detail: str = "") -> None:
        events.append(
            LifecycleEvent(
                attempt_id=attempt_id,
                session_id=session_id,
                role=role,
                phase=phase,
                elapsed=clock() - start,
                detail=detail,
            )
        )

    def finish(outcome: AttemptOutcome, error: str | None = None) -> AttemptResult:
        emit(LifecyclePhase.ATTEMPT_FINISHED, outcome.value)
        return AttemptResult(
            attempt_id=attempt_id,
            session_id=session_id,
            role=role,
            outcome=outcome,
            duration=clock() - start,
            events=events,
            error=redact(error) if error else None,
        )

    emit(LifecyclePhase.ATTEMPT_STARTED, f"resume={resume}")

    # Refuse to resume a tainted session unless an operator explicitly overrides.
    if resume and taint_store.is_tainted(session_id) and not allow_tainted_resume:
        emit(LifecyclePhase.SESSION_TAINTED, "resume refused: session previously tainted")
        raise TaintedSessionError(
            f"session {session_id} is tainted; refusing resume "
            f"(clear taint or start a fresh session to recover)"
        )

    # Open (create or resume).
    try:
        ops.open(resume=resume)
        emit(LifecyclePhase.SESSION_OPENED, "resumed" if resume else "created")
    except SdkError as e:
        return finish(AttemptOutcome.SDK_ERROR, str(e))

    # Run the prompt to idle, or hit the wait bound.
    try:
        signal = ops.run(prompt, cfg.wait_timeout)
    except SdkError as e:
        _safe_disconnect(ops, emit)
        return finish(AttemptOutcome.SDK_ERROR, str(e))

    if signal is RunSignal.IDLE:
        _safe_disconnect(ops, emit)
        return finish(AttemptOutcome.IDLE_COMPLETED)

    # Wait timed out — NOT a cancellation yet. Issue a bounded abort.
    emit(LifecyclePhase.WAIT_TIMED_OUT, f"no idle within {cfg.wait_timeout:.0f}s")
    emit(LifecyclePhase.ABORT_REQUESTED, f"abort bound {cfg.abort_timeout:.0f}s")
    try:
        confirmed = ops.abort(cfg.abort_timeout)
    except SdkError as e:
        confirmed = False
        emit(LifecyclePhase.ABORT_FAILED, f"abort raised: {e}")

    if confirmed:
        emit(LifecyclePhase.ABORT_CONFIRMED, "session idle after abort")
        _safe_disconnect(ops, emit)
        return finish(AttemptOutcome.ABORTED_CLEAN)

    # Abort could not be confirmed → force stop and taint.
    emit(LifecyclePhase.ABORT_FAILED, "abort not confirmed within bound")
    try:
        ops.force_stop()
    except SdkError as e:
        emit(LifecyclePhase.FORCE_STOPPED, f"force_stop error (still tainting): {e}")
    else:
        emit(LifecyclePhase.FORCE_STOPPED, "runtime force-stopped")
    taint_store.taint(session_id)
    emit(LifecyclePhase.SESSION_TAINTED, "session marked unsafe to resume")
    return finish(AttemptOutcome.TAINTED, "wait timed out; abort unconfirmed; session tainted")


def _safe_disconnect(ops: SdkOps, emit) -> None:
    try:
        ops.disconnect()
        emit(LifecyclePhase.DISCONNECTED, "clean")
    except SdkError as e:
        emit(LifecyclePhase.DISCONNECTED, f"disconnect error: {e}")


def _new_attempt_id(session_id: str, clock) -> str:
    stamp = int(clock() * 1000) & 0xFFFFFF
    tag = hashlib.sha256(f"{session_id}:{stamp}".encode()).hexdigest()[:8]
    return f"att-{tag}"
