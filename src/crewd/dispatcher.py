"""Durable, Lead-directed dispatch kernel (issue #11, slice A).

This module owns the *orchestration* state of a goal run: which role Lead has
routed to, what attempts have been reserved/started/finished, the immutable
handoffs those attempts produce, and Lead's structured routing decisions. It is
the crash-consistent source of truth that replaces the fixed round-robin cycle
counter.

Design decisions (see issue #11 and the Advisory analysis):

* **One SQLite journal, one writer.** All couplings that must be atomic —
  recording a terminal outcome *and* its handoff *and* handing routing authority
  back to Lead; or acknowledging exactly the input handoffs *and* creating the
  next dispatch — happen inside a single SQLite transaction. SQLite's atomic
  commit gives all-or-nothing durability across crash/power loss, so there is no
  multi-file reconciliation protocol. The rollback journal is sufficient for a
  single dispatcher writer.

* **Typed outcomes, never exit-code inference.** Terminal attempt outcomes are
  :class:`~crewd.session_backend.AttemptOutcome` values produced by the backend;
  the higher-level :class:`HandoffOutcome` class is explicit. The
  ``.attempt.json`` sidecar remains a derived observability export, never the
  source of truth.

* **Durable pre-invocation reservation.** A work slot is reserved *before* the
  executor is invoked, so a crash/restart can never refund budget. Max-work is
  counted in reserved slots.

* **At-least-once handoffs, idempotent acknowledgement.** A handoff is never
  marked consumed merely because it was placed in a prompt; a crash after an SDK
  send but before commit would then lose it. Handoffs carry stable ids and are
  acknowledged only by Lead's next structured decision. Re-acknowledging an
  already-consumed handoff is a no-op.

* **Restart reconciliation, never auto-replay.** A ``started`` (or merely
  ``reserved``) attempt with no terminal record is reconciled to ``uncertain``
  and returned to Lead; it is never silently re-run.

This slice is intentionally wired into nothing: it introduces no change to the
production loop. Slice B integrates it with the SDK backend and the CLI.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional

from .session_backend import AttemptOutcome


# ─────────────────────────── typed vocabulary ───────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class DecisionKind(str, Enum):
    """The closed set of routing decisions Lead may make after every handoff."""

    DISPATCH = "dispatch"            # invoke a named role
    CONTINUE_LEAD = "continue_lead"  # Lead keeps working itself (another Lead turn)
    WAIT = "wait"                    # persist a wake condition, launch no role
    PAUSE = "pause"                  # human blocker; stop launching until resumed
    FINISH = "finish"               # final acceptance; the goal run is complete


class RunStatus(str, Enum):
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    FINISHED = "finished"
    STOPPED = "stopped"
    EXHAUSTED = "exhausted"     # durable work budget exhausted
    INTERRUPTED = "interrupted"


class AttemptState(str, Enum):
    RESERVED = "reserved"                    # slot durably reserved, executor not yet invoked
    STARTED = "started"                      # executor invoked, no terminal record yet
    TERMINAL = "terminal"                    # exactly one terminal outcome recorded
    RECONCILED_UNCERTAIN = "reconciled_uncertain"  # restart found no terminal record


class HandoffOutcome(str, Enum):
    """Outcome *class* of a completed attempt, from the dispatcher's view.

    Distinct from :class:`AttemptOutcome` (which is SDK-lifecycle specific): this
    is what Lead routes on. ``NO_PROGRESS`` is explicit rather than inferred; the
    semantic progress token that distinguishes real progress from a bare
    completion is defined by #12.
    """

    COMPLETED = "completed"      # productive completion
    NO_PROGRESS = "no_progress"  # completed but produced no progress
    FAILED = "failed"            # error terminal
    TIMED_OUT = "timed_out"      # wait bound exceeded (with or without clean abort)
    CANCELLED = "cancelled"      # operator/ signal cancellation
    UNCERTAIN = "uncertain"      # taint or restart-reconciled; safe state unknown

    @property
    def is_unproductive(self) -> bool:
        """Whether this outcome counts toward the consecutive-unproductive cap."""
        return self in (
            HandoffOutcome.NO_PROGRESS,
            HandoffOutcome.FAILED,
            HandoffOutcome.TIMED_OUT,
            HandoffOutcome.UNCERTAIN,
        )


def classify(outcome: AttemptOutcome) -> HandoffOutcome:
    """Default mapping from an SDK :class:`AttemptOutcome` to a handoff class.

    Callers may override with an explicit class (e.g. a completed attempt that a
    role self-reports as ``NO_PROGRESS``).
    """
    return {
        AttemptOutcome.IDLE_COMPLETED: HandoffOutcome.COMPLETED,
        AttemptOutcome.SDK_ERROR: HandoffOutcome.FAILED,
        AttemptOutcome.ABORTED_CLEAN: HandoffOutcome.TIMED_OUT,
        AttemptOutcome.TAINTED: HandoffOutcome.UNCERTAIN,
    }[outcome]


# ─────────────────────────── decisions & views ───────────────────────────
@dataclass(frozen=True)
class LeadDecision:
    """A structured decision produced by Lead after consuming handoffs.

    ``ack_handoff_ids`` are the exact input handoffs this decision consumes. They
    are acknowledged atomically with whatever the decision creates.
    """

    kind: DecisionKind
    ack_handoff_ids: tuple[str, ...] = ()
    role: Optional[str] = None            # required for DISPATCH
    reason: Optional[str] = None
    wake_condition: Optional[str] = None  # for WAIT
    human_blocker: Optional[str] = None   # for PAUSE
    final_acceptance: Optional[str] = None  # for FINISH

    @staticmethod
    def dispatch(role: str, *, ack: tuple[str, ...] = (), reason: str | None = None) -> "LeadDecision":
        return LeadDecision(DecisionKind.DISPATCH, ack_handoff_ids=ack, role=role, reason=reason)

    @staticmethod
    def continue_lead(*, ack: tuple[str, ...] = (), reason: str | None = None) -> "LeadDecision":
        return LeadDecision(DecisionKind.CONTINUE_LEAD, ack_handoff_ids=ack, role="lead", reason=reason)

    @staticmethod
    def wait(condition: str, *, ack: tuple[str, ...] = (), reason: str | None = None) -> "LeadDecision":
        return LeadDecision(DecisionKind.WAIT, ack_handoff_ids=ack, wake_condition=condition, reason=reason)

    @staticmethod
    def pause(blocker: str, *, ack: tuple[str, ...] = ()) -> "LeadDecision":
        return LeadDecision(DecisionKind.PAUSE, ack_handoff_ids=ack, human_blocker=blocker)

    @staticmethod
    def finish(acceptance: str, *, ack: tuple[str, ...] = ()) -> "LeadDecision":
        return LeadDecision(DecisionKind.FINISH, ack_handoff_ids=ack, final_acceptance=acceptance)


class DecisionError(ValueError):
    """A Lead decision was invalid; routing authority stays with Lead."""


@dataclass(frozen=True)
class HandoffView:
    id: str
    attempt_id: str
    run_id: str
    role: str
    outcome_class: HandoffOutcome
    evidence: str
    changed: str
    remaining: str
    reason_returned: str
    created_at: str
    consumed_by_dispatch_id: Optional[str]


@dataclass(frozen=True)
class DispatchView:
    id: str
    run_id: str
    seq: int
    kind: DecisionKind
    role: Optional[str]
    reason: Optional[str]
    created_at: str


@dataclass(frozen=True)
class DecisionResult:
    """Outcome of :meth:`Dispatcher.lead_decide`.

    ``dispatch`` is the created dispatch for ``DISPATCH`` / ``CONTINUE_LEAD`` (and
    is recorded for ``WAIT`` / ``PAUSE`` / ``FINISH`` too, as an audit entry).
    ``guard_tripped`` is True when a thrash/no-progress bound converted a routing
    decision into a synthetic pause instead of dispatching.
    """

    run: "RunView"
    dispatch: Optional[DispatchView] = None
    guard_tripped: bool = False
    guard_reason: Optional[str] = None


@dataclass(frozen=True)
class AttemptView:
    id: str
    dispatch_id: str
    run_id: str
    role: str
    session_id: Optional[str]
    generation: int
    state: AttemptState
    terminal_outcome: Optional[AttemptOutcome]


@dataclass(frozen=True)
class RunView:
    id: str
    goal_label: str
    status: RunStatus
    routing_authority: str          # "lead_pending" or a dispatch id currently in flight
    reserved_slots: int
    consecutive_unproductive: int
    last_edge: Optional[str]
    last_edge_repeats: int
    wake_condition: Optional[str]
    human_blocker: Optional[str]


@dataclass(frozen=True)
class DispatcherLimits:
    """Deterministic bounds. ``0`` means unbounded."""

    max_work: int = 0                 # total attempt slots reservable per run
    max_consecutive_unproductive: int = 5
    max_edge_repeats: int = 3         # identical role edge repeated w/o new progress/handoff


LEAD_PENDING = "lead_pending"


# ─────────────────────────── the kernel ───────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_run (
    id TEXT PRIMARY KEY,
    goal_label TEXT NOT NULL,
    status TEXT NOT NULL,
    routing_authority TEXT NOT NULL,
    reserved_slots INTEGER NOT NULL DEFAULT 0,
    consecutive_unproductive INTEGER NOT NULL DEFAULT 0,
    last_edge TEXT,
    last_edge_repeats INTEGER NOT NULL DEFAULT 0,
    wake_condition TEXT,
    human_blocker TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES goal_run(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    role TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt (
    id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL REFERENCES dispatch(id),
    run_id TEXT NOT NULL REFERENCES goal_run(id),
    role TEXT NOT NULL,
    session_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    terminal_outcome TEXT,
    reserved_at TEXT NOT NULL,
    started_at TEXT,
    terminal_at TEXT
);
CREATE TABLE IF NOT EXISTS handoff (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempt(id),
    run_id TEXT NOT NULL REFERENCES goal_run(id),
    role TEXT NOT NULL,
    outcome_class TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    changed TEXT NOT NULL DEFAULT '',
    remaining TEXT NOT NULL DEFAULT '',
    reason_returned TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    consumed_by_dispatch_id TEXT REFERENCES dispatch(id)
);
CREATE INDEX IF NOT EXISTS ix_handoff_run_unconsumed
    ON handoff(run_id) WHERE consumed_by_dispatch_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_attempt_run_state ON attempt(run_id, state);
CREATE INDEX IF NOT EXISTS ix_dispatch_run_seq ON dispatch(run_id, seq);
"""


class BudgetExhausted(RuntimeError):
    """The durable work budget for this run is exhausted."""


class Dispatcher:
    """Crash-consistent orchestration store for a single workspace.

    A single :class:`Dispatcher` instance is the single writer. Every mutating
    method commits (or rolls back) as one transaction.
    """

    def __init__(self, db_path: Path, limits: DispatcherLimits | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = limits or DispatcherLimits()
        # isolation_level=None → autocommit off via explicit BEGIN in _txn.
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA synchronous = FULL;")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Dispatcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction; commit on success, rollback on error."""
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            yield self._conn
            self._conn.execute("COMMIT;")
        except BaseException:
            self._conn.execute("ROLLBACK;")
            raise

    # ── run lifecycle ────────────────────────────────────────────
    def start_or_resume_run(self, goal_label: str) -> RunView:
        """Return the active run for ``goal_label``, creating one if absent.

        A *new goal epoch* (different label with no active run) always creates a
        fresh run — prior epochs are never resumed. Routing authority starts
        with Lead, which makes the first decision.
        """
        with self._txn() as c:
            row = c.execute(
                "SELECT * FROM goal_run WHERE goal_label = ? AND status = ? LIMIT 1",
                (goal_label, RunStatus.ACTIVE.value),
            ).fetchone()
            if row is None:
                run_id = _new_id("run")
                c.execute(
                    "INSERT INTO goal_run (id, goal_label, status, routing_authority, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, goal_label, RunStatus.ACTIVE.value, LEAD_PENDING, _now()),
                )
                row = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        return _run_view(row)

    def get_run(self, run_id: str) -> RunView:
        row = self._conn.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _run_view(row)

    # ── work reservation & execution bookkeeping ─────────────────
    def reserve_attempt(self, run_id: str, dispatch_id: str, role: str) -> str:
        """Durably reserve a work slot BEFORE the executor is invoked.

        Raises :class:`BudgetExhausted` (marking the run EXHAUSTED) if the
        max-work budget would be exceeded. The reservation is counted whether or
        not the executor later starts, so a crash cannot refund the slot.
        """
        with self._txn() as c:
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            limit = self.limits.max_work
            over_budget = bool(limit) and run["reserved_slots"] >= limit
            if over_budget:
                c.execute(
                    "UPDATE goal_run SET status = ? WHERE id = ?",
                    (RunStatus.EXHAUSTED.value, run_id),
                )
                attempt_id = None
            else:
                attempt_id = _new_id("att")
                c.execute(
                    "INSERT INTO attempt (id, dispatch_id, run_id, role, state, reserved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (attempt_id, dispatch_id, run_id, role, AttemptState.RESERVED.value, _now()),
                )
                c.execute(
                    "UPDATE goal_run SET reserved_slots = reserved_slots + 1, routing_authority = ? "
                    "WHERE id = ?",
                    (dispatch_id, run_id),
                )
        # Raise only after the EXHAUSTED status is durably committed, so a crash
        # cannot leave the run looking active with no remaining budget.
        if attempt_id is None:
            raise BudgetExhausted(f"max_work={self.limits.max_work} reached for run {run_id}")
        return attempt_id

    def mark_started(self, attempt_id: str, *, session_id: str, generation: int) -> None:
        """Record that the executor was invoked (RESERVED → STARTED)."""
        with self._txn() as c:
            cur = c.execute(
                "UPDATE attempt SET state = ?, session_id = ?, generation = ?, started_at = ? "
                "WHERE id = ? AND state = ?",
                (
                    AttemptState.STARTED.value,
                    session_id,
                    generation,
                    _now(),
                    attempt_id,
                    AttemptState.RESERVED.value,
                ),
            )
            if cur.rowcount != 1:
                raise DecisionError(f"attempt {attempt_id} not in reserved state")

    def record_terminal(
        self,
        attempt_id: str,
        outcome: AttemptOutcome,
        *,
        outcome_class: HandoffOutcome | None = None,
        evidence: str = "",
        changed: str = "",
        remaining: str = "",
        reason_returned: str = "",
    ) -> str:
        """Atomically record the single terminal outcome, its immutable handoff,
        and hand routing authority back to Lead.

        Returns the new handoff id. Idempotent per attempt: recording a terminal
        outcome for an attempt that already has one raises
        :class:`DecisionError` (exactly-one-terminal invariant).
        """
        cls = outcome_class or classify(outcome)
        with self._txn() as c:
            att = c.execute("SELECT * FROM attempt WHERE id = ?", (attempt_id,)).fetchone()
            if att is None:
                raise KeyError(attempt_id)
            if att["state"] == AttemptState.TERMINAL.value:
                raise DecisionError(f"attempt {attempt_id} already terminal")
            if att["state"] not in (AttemptState.RESERVED.value, AttemptState.STARTED.value):
                raise DecisionError(f"attempt {attempt_id} not in-flight ({att['state']})")
            c.execute(
                "UPDATE attempt SET state = ?, terminal_outcome = ?, terminal_at = ? WHERE id = ?",
                (AttemptState.TERMINAL.value, outcome.value, _now(), attempt_id),
            )
            handoff_id = self._insert_handoff(
                c, att["run_id"], attempt_id, att["role"], cls,
                evidence, changed, remaining, reason_returned,
            )
            self._return_to_lead(c, att["run_id"], cls)
        return handoff_id

    # ── restart reconciliation ───────────────────────────────────
    def reconcile_on_restart(self, run_id: str) -> list[str]:
        """Reconcile in-flight attempts after a crash.

        Any attempt still ``reserved`` or ``started`` (no terminal record) is
        marked ``reconciled_uncertain`` and emits an ``uncertain`` handoff, then
        routing authority returns to Lead. Never auto-replays. Idempotent.
        """
        created: list[str] = []
        with self._txn() as c:
            rows = c.execute(
                "SELECT * FROM attempt WHERE run_id = ? AND state IN (?, ?)",
                (run_id, AttemptState.RESERVED.value, AttemptState.STARTED.value),
            ).fetchall()
            for att in rows:
                c.execute(
                    "UPDATE attempt SET state = ?, terminal_at = ? WHERE id = ?",
                    (AttemptState.RECONCILED_UNCERTAIN.value, _now(), att["id"]),
                )
                handoff_id = self._insert_handoff(
                    c, run_id, att["id"], att["role"], HandoffOutcome.UNCERTAIN,
                    evidence="", changed="",
                    remaining="in-flight attempt found with no terminal record after restart",
                    reason_returned="restart_reconciliation",
                )
                created.append(handoff_id)
            if rows:
                self._return_to_lead(c, run_id, HandoffOutcome.UNCERTAIN)
        return created

    # ── Lead decisions ───────────────────────────────────────────
    def pending_handoffs(self, run_id: str) -> list[HandoffView]:
        rows = self._conn.execute(
            "SELECT * FROM handoff WHERE run_id = ? AND consumed_by_dispatch_id IS NULL "
            "ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [_handoff_view(r) for r in rows]

    def lead_decide(self, run_id: str, decision: LeadDecision, *, configured_roles) -> DecisionResult:
        """Apply a structured Lead decision atomically.

        Every decision is recorded as a ``dispatch`` row (audit + acknowledgement
        anchor). Acknowledges exactly ``decision.ack_handoff_ids`` (idempotently)
        against that dispatch. ``DISPATCH`` / ``CONTINUE_LEAD`` set routing
        authority to the new dispatch (in-flight); ``WAIT`` / ``PAUSE`` /
        ``FINISH`` return authority to Lead, update run status, and launch no
        role.

        Invalid decisions raise :class:`DecisionError` and leave all state
        unchanged (transaction rolls back). Thrash guard: exceeding
        ``max_consecutive_unproductive``, or repeating an identical role edge more
        than ``max_edge_repeats`` times without an intervening productive handoff,
        does **not** dispatch; it emits one synthetic handoff, pauses the run, and
        returns ``guard_tripped=True`` (the acks are not applied so Lead re-decides
        after the human unblocks).
        """
        configured = set(configured_roles)
        with self._txn() as c:
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)

            if decision.kind is DecisionKind.DISPATCH:
                if not decision.role or decision.role not in configured:
                    raise DecisionError(f"dispatch target {decision.role!r} is not a configured role")

            # Validate ack targets up front (all-or-nothing) before any mutation.
            self._validate_acks(c, run_id, decision.ack_handoff_ids)

            routing = decision.kind in (DecisionKind.DISPATCH, DecisionKind.CONTINUE_LEAD)
            if routing:
                edge = decision.role or "lead"
                tripped, reason = self._thrash_reason(run, edge)
                if tripped:
                    self._synthetic_pause(c, run_id, reason)
                    return DecisionResult(
                        run=_run_view(c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()),
                        guard_tripped=True,
                        guard_reason=reason,
                    )

            dispatch = self._create_dispatch(c, run_id, decision)
            self._apply_acks(c, decision.ack_handoff_ids, dispatch.id)

            if routing:
                edge = decision.role or "lead"
                repeats = (run["last_edge_repeats"] + 1) if run["last_edge"] == edge else 1
                c.execute(
                    "UPDATE goal_run SET routing_authority = ?, last_edge = ?, last_edge_repeats = ? "
                    "WHERE id = ?",
                    (dispatch.id, edge, repeats, run_id),
                )
            else:
                status = {
                    DecisionKind.WAIT: RunStatus.WAITING,
                    DecisionKind.PAUSE: RunStatus.PAUSED,
                    DecisionKind.FINISH: RunStatus.FINISHED,
                }[decision.kind]
                c.execute(
                    "UPDATE goal_run SET status = ?, routing_authority = ?, wake_condition = ?, "
                    "human_blocker = ? WHERE id = ?",
                    (status.value, LEAD_PENDING, decision.wake_condition, decision.human_blocker, run_id),
                )
            return DecisionResult(
                run=_run_view(c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()),
                dispatch=dispatch,
            )

    # ── internal helpers ─────────────────────────────────────────
    def _insert_handoff(self, c, run_id, attempt_id, role, cls: HandoffOutcome,
                        evidence, changed, remaining, reason_returned) -> str:
        handoff_id = _new_id("ho")
        c.execute(
            "INSERT INTO handoff (id, attempt_id, run_id, role, outcome_class, evidence, "
            "changed, remaining, reason_returned, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (handoff_id, attempt_id, run_id, role, cls.value, evidence, changed,
             remaining, reason_returned, _now()),
        )
        return handoff_id

    def _return_to_lead(self, c, run_id: str, cls: HandoffOutcome) -> None:
        """Hand routing authority back to Lead and update the unproductive counter."""
        run = c.execute("SELECT consecutive_unproductive FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        unproductive = (run["consecutive_unproductive"] + 1) if cls.is_unproductive else 0
        c.execute(
            "UPDATE goal_run SET routing_authority = ?, consecutive_unproductive = ? WHERE id = ?",
            (LEAD_PENDING, unproductive, run_id),
        )

    def _validate_acks(self, c, run_id: str, ack_ids) -> None:
        for hid in ack_ids:
            row = c.execute(
                "SELECT run_id FROM handoff WHERE id = ?", (hid,)
            ).fetchone()
            if row is None:
                raise DecisionError(f"unknown handoff id {hid!r}")
            if row["run_id"] != run_id:
                raise DecisionError(f"handoff {hid!r} belongs to a different run")

    def _apply_acks(self, c, ack_ids, dispatch_id: str) -> None:
        # Idempotent: only unconsumed handoffs are acknowledged.
        for hid in ack_ids:
            c.execute(
                "UPDATE handoff SET consumed_by_dispatch_id = ? "
                "WHERE id = ? AND consumed_by_dispatch_id IS NULL",
                (dispatch_id, hid),
            )

    def _thrash_reason(self, run, edge: str) -> tuple[bool, Optional[str]]:
        if self.limits.max_consecutive_unproductive and (
            run["consecutive_unproductive"] >= self.limits.max_consecutive_unproductive
        ):
            return True, (
                f"{run['consecutive_unproductive']} consecutive unproductive attempts "
                f"(cap {self.limits.max_consecutive_unproductive})"
            )
        if self.limits.max_edge_repeats and run["last_edge"] == edge and (
            run["last_edge_repeats"] >= self.limits.max_edge_repeats
        ):
            return True, (
                f"routing edge {edge!r} repeated {run['last_edge_repeats']} times "
                f"without new progress (cap {self.limits.max_edge_repeats})"
            )
        return False, None

    def _synthetic_pause(self, c, run_id: str, blocker: str) -> None:
        """Emit one synthetic handoff and pause, rather than looping into Lead forever."""
        att = c.execute(
            "SELECT id, role FROM attempt WHERE run_id = ? ORDER BY reserved_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if att is not None:
            self._insert_handoff(
                c, run_id, att["id"], att["role"], HandoffOutcome.UNCERTAIN,
                evidence="", changed="", remaining=blocker, reason_returned="thrash_guard",
            )
        c.execute(
            "UPDATE goal_run SET status = ?, routing_authority = ?, human_blocker = ? WHERE id = ?",
            (RunStatus.PAUSED.value, LEAD_PENDING, blocker, run_id),
        )

    def _create_dispatch(self, c, run_id: str, decision: LeadDecision) -> DispatchView:
        seq_row = c.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM dispatch WHERE run_id = ?", (run_id,)
        ).fetchone()
        seq = seq_row["m"] + 1
        dispatch_id = _new_id("dsp")
        c.execute(
            "INSERT INTO dispatch (id, run_id, seq, kind, role, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dispatch_id, run_id, seq, decision.kind.value, decision.role, decision.reason, _now()),
        )
        row = c.execute("SELECT * FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()
        return _dispatch_view(row)

    # ── read helpers (test/observability) ────────────────────────
    def get_attempt(self, attempt_id: str) -> AttemptView:
        row = self._conn.execute("SELECT * FROM attempt WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return _attempt_view(row)

    def export_run(self, run_id: str) -> dict:
        """A derived, human-readable snapshot (never the source of truth)."""
        run = self.get_run(run_id)
        atts = self._conn.execute(
            "SELECT * FROM attempt WHERE run_id = ? ORDER BY reserved_at", (run_id,)
        ).fetchall()
        hos = self._conn.execute(
            "SELECT * FROM handoff WHERE run_id = ? ORDER BY created_at", (run_id,)
        ).fetchall()
        return {
            "run": run.__dict__,
            "attempts": [dict(a) for a in atts],
            "handoffs": [dict(h) for h in hos],
        }


# ─────────────────────────── row → view ───────────────────────────
def _run_view(row: sqlite3.Row) -> RunView:
    return RunView(
        id=row["id"],
        goal_label=row["goal_label"],
        status=RunStatus(row["status"]),
        routing_authority=row["routing_authority"],
        reserved_slots=row["reserved_slots"],
        consecutive_unproductive=row["consecutive_unproductive"],
        last_edge=row["last_edge"],
        last_edge_repeats=row["last_edge_repeats"],
        wake_condition=row["wake_condition"],
        human_blocker=row["human_blocker"],
    )


def _dispatch_view(row: sqlite3.Row) -> DispatchView:
    return DispatchView(
        id=row["id"], run_id=row["run_id"], seq=row["seq"], kind=DecisionKind(row["kind"]),
        role=row["role"], reason=row["reason"], created_at=row["created_at"],
    )


def _attempt_view(row: sqlite3.Row) -> AttemptView:
    return AttemptView(
        id=row["id"], dispatch_id=row["dispatch_id"], run_id=row["run_id"], role=row["role"],
        session_id=row["session_id"], generation=row["generation"],
        state=AttemptState(row["state"]),
        terminal_outcome=AttemptOutcome(row["terminal_outcome"]) if row["terminal_outcome"] else None,
    )


def _handoff_view(row: sqlite3.Row) -> HandoffView:
    return HandoffView(
        id=row["id"], attempt_id=row["attempt_id"], run_id=row["run_id"], role=row["role"],
        outcome_class=HandoffOutcome(row["outcome_class"]), evidence=row["evidence"],
        changed=row["changed"], remaining=row["remaining"], reason_returned=row["reason_returned"],
        created_at=row["created_at"], consumed_by_dispatch_id=row["consumed_by_dispatch_id"],
    )
