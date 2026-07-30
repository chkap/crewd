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

import json
import sqlite3
import threading
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
    SOLICIT_LEAD = "solicit_lead"   # a journaled, budgeted turn that asks Lead to decide


class DispatchIntent(str, Enum):
    """The mode a dispatch is routed under (issue #64).

    Gate policy is intent-aware: a normal implementation review keeps the
    canonical linked-PR + Worker-readiness safeguards, while a Lead-assigned
    verifier-only audit / acceptance / release / advisory consult must not
    require fictitious Worker implementation work. Legacy / untyped dispatches
    read as :attr:`IMPLEMENTATION` — the conservative default that preserves the
    strongest safeguards for any record written before this column existed.
    """

    IMPLEMENTATION = "implementation"   # normal implement→review (full safeguards)
    VERIFIER_AUDIT = "verifier_audit"   # Lead-assigned read-only audit (no fake Worker work)
    ACCEPTANCE = "acceptance"           # acceptance verification (no fake Worker work)
    RELEASE = "release"                 # release / post-publication verification
    ADVISORY = "advisory"               # non-binding advisory consult

    @classmethod
    def coerce(cls, raw) -> "DispatchIntent":
        """Coerce an untrusted / legacy value to a known intent.

        ``None``, empty, or an unrecognised value → :attr:`IMPLEMENTATION` (the
        conservative default), so a missing/legacy record never silently drops a
        Worker-readiness safeguard.
        """
        if isinstance(raw, cls):
            return raw
        if not raw:
            return cls.IMPLEMENTATION
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return cls.IMPLEMENTATION


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
            HandoffOutcome.CANCELLED,
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
        AttemptOutcome.CANCELLED_CLEAN: HandoffOutcome.CANCELLED,
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
    task_number: Optional[int] = None     # the exact crewd:task this DISPATCH targets
    intent: "DispatchIntent" = None       # the mode this DISPATCH is routed under (#64)

    @staticmethod
    def dispatch(
        role: str,
        *,
        ack: tuple[str, ...] = (),
        reason: str | None = None,
        task_number: int | None = None,
        intent: "DispatchIntent | str | None" = None,
    ) -> "LeadDecision":
        return LeadDecision(
            DecisionKind.DISPATCH, ack_handoff_ids=ack, role=role, reason=reason,
            task_number=task_number, intent=DispatchIntent.coerce(intent),
        )

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
    disagreement: str
    blocker: str
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
    task_number: Optional[int] = None
    pr_number: Optional[int] = None
    intent: "DispatchIntent" = None


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
    solicitation_invalid: bool = False
    invalid_reason: Optional[str] = None
    gate_corrected: bool = False
    gate_correction_reason: Optional[str] = None


@dataclass(frozen=True)
class Solicitation:
    """A journaled, budgeted request for a Lead decision.

    Opening a solicitation reserves exactly one Lead attempt (consuming work
    budget) bound to a ``solicit_lead`` dispatch that owns routing authority.
    The solicitation captures the authority nonce (``authority_seq``) and the
    *exact* set of pending handoff ids Lead is being asked to route. The Lead
    turn's decision is only ever consumed once, through
    :meth:`Dispatcher.resolve_lead_solicitation`, which requires the nonce to be
    unchanged and the decision to acknowledge exactly ``pending_handoff_ids``.
    A crash after the Lead turn but before resolution loses the in-memory
    candidate decision; restart reconciliation taints the journaled attempt and
    the decision is never recovered from any file.
    """

    attempt_id: str
    dispatch_id: str
    run_id: str
    authority_seq: int
    pending_handoff_ids: tuple[str, ...]


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
    authority_seq: int = 0
    invalid_solicitations: int = 0
    consecutive_corrections: int = 0
    gate_correction: Optional[str] = None


@dataclass(frozen=True)
class RunDiagnostics:
    """Read-only, single-transaction projection of a run's durable state.

    Produced by :meth:`Dispatcher.read_run_diagnostics`. Pure durable truth from
    the dispatch journal — control/liveness/session facts are layered on top at
    explicitly lower authority by :mod:`crewd.diagnostics`.
    """

    run: "RunView"
    current_attempt: "Optional[AttemptView]"
    latest_handoff: "Optional[HandoffView]"
    pending_handoff_count: int
    latest_dispatch: "Optional[DispatchView]"


@dataclass(frozen=True)
class DispatcherLimits:
    """Deterministic bounds. ``0`` means unbounded."""

    max_work: int = 0                 # total attempt slots reservable per run
    max_consecutive_unproductive: int = 5
    max_edge_repeats: int = 3         # identical role edge repeated w/o new progress/handoff
    max_invalid_solicitations: int = 3  # invalid/failed Lead turns before a persisted pause
    max_gate_corrections: int = 3       # consecutive correctable gate rejections before escalation


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
    authority_seq INTEGER NOT NULL DEFAULT 0,
    invalid_solicitations INTEGER NOT NULL DEFAULT 0,
    consecutive_corrections INTEGER NOT NULL DEFAULT 0,
    gate_correction TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES goal_run(id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    role TEXT,
    reason TEXT,
    task_number INTEGER,
    pr_number INTEGER,
    intent TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt (
    id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE REFERENCES dispatch(id),
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
    disagreement TEXT NOT NULL DEFAULT '',
    blocker TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    consumed_by_dispatch_id TEXT REFERENCES dispatch(id)
);
CREATE INDEX IF NOT EXISTS ix_handoff_run_unconsumed
    ON handoff(run_id) WHERE consumed_by_dispatch_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_attempt_run_state ON attempt(run_id, state);
CREATE INDEX IF NOT EXISTS ix_dispatch_run_seq ON dispatch(run_id, seq);
CREATE TABLE IF NOT EXISTS solicitation (
    attempt_id TEXT PRIMARY KEY REFERENCES attempt(id),
    dispatch_id TEXT NOT NULL UNIQUE REFERENCES dispatch(id),
    run_id TEXT NOT NULL REFERENCES goal_run(id),
    authority_seq INTEGER NOT NULL,
    pending_ids TEXT NOT NULL,
    created_at TEXT NOT NULL
);
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
        # check_same_thread=False: the orchestrator journals `mark_started` from
        # the attempt's worker thread while the main thread polls controls. Access
        # is still serialized by ``self._lock`` (the single-writer invariant holds
        # — there is never truly concurrent DB use, only cross-thread use).
        self._conn = sqlite3.connect(
            str(self.db_path), isolation_level=None, check_same_thread=False
        )
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA synchronous = FULL;")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    # Bump when a schema change needs an in-place migration of existing DBs.
    _SCHEMA_VERSION = 5

    def _migrate(self) -> None:
        """Idempotently upgrade a database created by an earlier kernel version.

        ``CREATE TABLE IF NOT EXISTS`` in :data:`_SCHEMA` creates any *missing*
        tables (e.g. ``solicitation`` and its indexes on a pre-#17 database) but
        never alters an *existing* table — so a database created by the merged
        #11 kernel lacks the ``authority_seq`` / ``invalid_solicitations``
        columns, and a database created by the merged #17 kernel lacks the #12
        ``disagreement`` / ``blocker`` handoff columns, and a pre-#47 database
        lacks the ``dispatch.task_number`` binding column — any would fail to
        decode. Add them in place with safe ``NOT NULL DEFAULT`` semantics (or a
        nullable column where a default would misrepresent history). The
        per-table column-existence check is the durable oracle (idempotent across
        repeated opens); ``user_version`` is a derived marker only. Runs as one
        transaction so a crash mid-upgrade leaves the database wholly upgraded or
        wholly not.
        """
        goal_run_cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(goal_run)").fetchall()
        }
        handoff_cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(handoff)").fetchall()
        }
        dispatch_cols = {
            r["name"] for r in self._conn.execute("PRAGMA table_info(dispatch)").fetchall()
        }
        additions = [
            (goal_run_cols, "authority_seq",
             "ALTER TABLE goal_run ADD COLUMN authority_seq INTEGER NOT NULL DEFAULT 0"),
            (goal_run_cols, "invalid_solicitations",
             "ALTER TABLE goal_run ADD COLUMN invalid_solicitations INTEGER NOT NULL DEFAULT 0"),
            (handoff_cols, "disagreement",
             "ALTER TABLE handoff ADD COLUMN disagreement TEXT NOT NULL DEFAULT ''"),
            (handoff_cols, "blocker",
             "ALTER TABLE handoff ADD COLUMN blocker TEXT NOT NULL DEFAULT ''"),
            # #47: the exact routed task bound to a dispatch (nullable — historic
            # dispatches and non-task dispatches like solicit/continue have none).
            (dispatch_cols, "task_number",
             "ALTER TABLE dispatch ADD COLUMN task_number INTEGER"),
            (dispatch_cols, "pr_number",
             "ALTER TABLE dispatch ADD COLUMN pr_number INTEGER"),
            # #64: durable dispatch intent (nullable — a legacy/untyped row reads
            # as IMPLEMENTATION via DispatchIntent.coerce, the conservative
            # default that keeps the strongest safeguards); and the bounded
            # gate-correction state that lets a correctable rejection return to
            # Lead instead of being mislabeled as a human blocker.
            (dispatch_cols, "intent",
             "ALTER TABLE dispatch ADD COLUMN intent TEXT"),
            (goal_run_cols, "consecutive_corrections",
             "ALTER TABLE goal_run ADD COLUMN consecutive_corrections INTEGER NOT NULL DEFAULT 0"),
            (goal_run_cols, "gate_correction",
             "ALTER TABLE goal_run ADD COLUMN gate_correction TEXT"),
        ]
        pending = [sql for cols, name, sql in additions if name not in cols]
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            for sql in pending:
                self._conn.execute(sql)
            self._conn.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION};")
            self._conn.execute("COMMIT;")
        except BaseException:
            self._conn.execute("ROLLBACK;")
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Dispatcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """One IMMEDIATE transaction; commit on success, rollback on error.

        Lock-guarded so a cross-thread ``mark_started`` (worker thread) can never
        interleave with a main-thread transaction.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE;")
            try:
                yield self._conn
                self._conn.execute("COMMIT;")
            except BaseException:
                self._conn.execute("ROLLBACK;")
                raise

    # ── run lifecycle ────────────────────────────────────────────
    def start_or_resume_run(self, goal_label: str) -> RunView:
        """Return the run for ``goal_label``, creating one only if none exists.

        A *new goal epoch* (a label with no prior run) creates a fresh active
        run. For a label that already has a run, the existing run is returned
        **as-is, in whatever durable state it holds** — a restart must never
        silently create a second active run that bypasses a persisted
        pause/finish/exhausted/interrupted state. Reviving a non-active run
        requires the explicit :meth:`resume_run` transition.
        """
        with self._txn() as c:
            row = c.execute(
                "SELECT * FROM goal_run WHERE goal_label = ? ORDER BY created_at DESC LIMIT 1",
                (goal_label,),
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

    def resume_run(self, run_id: str) -> RunView:
        """Explicitly transition a paused/waiting/interrupted run back to active.

        This is the *only* way a non-active run resumes launching work; it clears
        the wake condition / human blocker and resets the thrash counters so the
        run does not immediately re-trip a guard after human intervention.
        Routing authority returns to Lead. ``finished`` and ``exhausted`` are
        terminal and cannot be resumed (raises :class:`DecisionError`); resuming
        an already-active run is a no-op.
        """
        resumable = {
            RunStatus.PAUSED.value,
            RunStatus.WAITING.value,
            RunStatus.INTERRUPTED.value,
            RunStatus.STOPPED.value,
        }
        with self._txn() as c:
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] == RunStatus.ACTIVE.value:
                return _run_view(run)
            if run["status"] not in resumable:
                raise DecisionError(f"run {run_id} is {run['status']} and cannot be resumed")
            c.execute(
                "UPDATE goal_run SET status = ?, routing_authority = ?, wake_condition = NULL, "
                "human_blocker = NULL, consecutive_unproductive = 0, last_edge = NULL, "
                "last_edge_repeats = 0, consecutive_corrections = 0, gate_correction = NULL, "
                "authority_seq = authority_seq + 1 WHERE id = ?",
                (RunStatus.ACTIVE.value, LEAD_PENDING, run_id),
            )
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        return _run_view(run)

    def get_run(self, run_id: str) -> RunView:
        row = self._conn.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _run_view(row)

    # ── work reservation & execution bookkeeping ─────────────────
    def reserve_attempt(self, run_id: str, dispatch_id: str, role: str) -> str:
        """Durably reserve a work slot BEFORE the executor is invoked.

        The dispatch is the authority: ``dispatch_id`` must exist, belong to
        ``run_id``, be a routing kind (``dispatch``/``continue_lead``), match
        ``role``, and currently own the run's routing authority. At most one
        attempt may be reserved per dispatch (also a ``UNIQUE`` DB backstop).
        These guards stop a stale caller after a crash from launching a second
        role while the journal says another dispatch owns authority, or from
        binding an attempt to the wrong run/role.

        Raises :class:`BudgetExhausted` (marking the run EXHAUSTED) if the
        max-work budget would be exceeded. The reservation is counted whether or
        not the executor later starts, so a crash cannot refund the slot.
        """
        with self._txn() as c:
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            dsp = c.execute("SELECT * FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()
            if dsp is None:
                raise DecisionError(f"unknown dispatch id {dispatch_id!r}")
            if dsp["run_id"] != run_id:
                raise DecisionError(f"dispatch {dispatch_id!r} belongs to a different run")
            if dsp["kind"] not in (DecisionKind.DISPATCH.value, DecisionKind.CONTINUE_LEAD.value):
                raise DecisionError(f"dispatch {dispatch_id!r} is not a routing decision")
            if (dsp["role"] or "lead") != role:
                raise DecisionError(
                    f"role {role!r} does not match dispatch role {dsp['role']!r}"
                )
            if run["routing_authority"] != dispatch_id:
                raise DecisionError(
                    f"dispatch {dispatch_id!r} does not own routing authority "
                    f"(current: {run['routing_authority']})"
                )
            existing = c.execute(
                "SELECT id FROM attempt WHERE dispatch_id = ?", (dispatch_id,)
            ).fetchone()
            if existing is not None:
                raise DecisionError(f"dispatch {dispatch_id!r} already has an attempt")

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
                    "UPDATE goal_run SET reserved_slots = reserved_slots + 1 WHERE id = ?",
                    (run_id,),
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
        disagreement: str = "",
        blocker: str = "",
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
            sol = c.execute(
                "SELECT 1 FROM solicitation WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if sol is not None:
                # A Lead solicitation attempt must never emit a handoff. Its
                # lifecycle is owned exclusively by resolve_lead_solicitation, so
                # candidate validation, invalid counters, nonce transitions, and
                # handoff suppression cannot be bypassed through this path.
                raise DecisionError(
                    f"attempt {attempt_id} is a Lead solicitation; terminalize it via "
                    "resolve_lead_solicitation, not record_terminal"
                )
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
                disagreement=disagreement, blocker=blocker,
            )
            self._return_to_lead(c, att["run_id"], cls)
        return handoff_id

    # ── restart reconciliation ───────────────────────────────────
    def reconcile_on_restart(self, run_id: str, *, taint_orphan=None) -> list[str]:
        """Reconcile in-flight attempts after a crash.

        Any attempt still ``reserved`` or ``started`` (no terminal record) is
        marked ``reconciled_uncertain`` and emits an ``uncertain`` handoff, then
        routing authority returns to Lead. Never auto-replays. Idempotent.

        ``taint_orphan(session_id, generation, role)``, when supplied, is invoked
        for every ``started`` orphan (an attempt that had already selected its
        session identity and could have reached the SDK transport) **before** the
        uncertain handoff is finalized. This durably poisons the orphaned session
        generation so it cannot be resumed *normally* on the next tick or after a
        second signal — recovery advances to a fresh generation instead. Because
        the taint is idempotent and the state transition below is a single atomic
        step, a crash *during* recovery is safe: re-running re-taints (a no-op) and
        then finalizes, so recovery itself is durable and retryable.
        """
        created: list[str] = []
        # Phase 1 — taint orphan sessions BEFORE any durable finalize. Ordered
        # first (and outside the finalizing txn) so we never finalize an uncertain
        # handoff while leaving its orphaned session silently resumable.
        if taint_orphan is not None:
            orphans = self._conn.execute(
                "SELECT id, role, session_id, generation FROM attempt "
                "WHERE run_id = ? AND state = ? AND session_id IS NOT NULL",
                (run_id, AttemptState.STARTED.value),
            ).fetchall()
            for o in orphans:
                taint_orphan(o["session_id"], int(o["generation"]), o["role"])
        with self._txn() as c:
            rows = c.execute(
                "SELECT a.*, d.kind AS dispatch_kind FROM attempt a "
                "JOIN dispatch d ON a.dispatch_id = d.id "
                "WHERE a.run_id = ? AND a.state IN (?, ?)",
                (run_id, AttemptState.RESERVED.value, AttemptState.STARTED.value),
            ).fetchall()
            for att in rows:
                c.execute(
                    "UPDATE attempt SET state = ?, terminal_at = ? WHERE id = ?",
                    (AttemptState.RECONCILED_UNCERTAIN.value, _now(), att["id"]),
                )
                # A journaled Lead solicitation owns exactly one Lead attempt and
                # never emits a role handoff; reconciling it must not synthesise
                # one. Its in-memory candidate decision (if the crash happened
                # after the Lead turn) is simply lost — never recovered from disk.
                if att["dispatch_kind"] == DecisionKind.SOLICIT_LEAD.value:
                    continue
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
        unchanged (transaction rolls back). A decision is only accepted when the
        run is ``active`` **and** routing authority is ``lead_pending`` — this
        enforces exclusive authority (no overlapping dispatches while one is
        in-flight) and refuses to launch work from a paused/finished/exhausted/
        interrupted run (revival requires :meth:`resume_run`). Thrash guard:
        exceeding ``max_consecutive_unproductive``, or repeating an identical role
        edge more than ``max_edge_repeats`` times without an intervening
        productive handoff, does **not** dispatch; it emits one synthetic handoff,
        pauses the run, and returns ``guard_tripped=True`` (the acks are not
        applied so Lead re-decides after the human unblocks).
        """
        configured = set(configured_roles)
        with self._txn() as c:
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)

            if run["status"] != RunStatus.ACTIVE.value:
                raise DecisionError(
                    f"run {run_id} is {run['status']}; resume it before deciding"
                )
            if run["routing_authority"] != LEAD_PENDING:
                raise DecisionError(
                    f"routing authority is held by {run['routing_authority']}, not Lead; "
                    "the in-flight attempt must reach a terminal outcome first"
                )

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
                    "human_blocker = ?, authority_seq = authority_seq + 1 WHERE id = ?",
                    (status.value, LEAD_PENDING, decision.wake_condition, decision.human_blocker, run_id),
                )
            return DecisionResult(
                run=_run_view(c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()),
                dispatch=dispatch,
            )

    # ── journaled Lead solicitation (issue #17) ──────────────────
    def open_lead_solicitation(self, run_id: str) -> Solicitation:
        """Reserve and journal a Lead decision turn *before* it is invoked.

        This is the durable, budgeted replacement for calling Lead's SDK session
        as un-journaled orchestration. It requires the run to be ``active`` with
        routing authority held by Lead (``lead_pending``). It creates a
        ``solicit_lead`` dispatch that takes routing authority and owns exactly
        one Lead attempt (consuming one work slot, so runaway Lead loops are
        bounded by ``max_work`` just like role attempts), and snapshots both the
        current authority nonce and the *exact* set of pending handoff ids Lead
        is being asked to route.

        The returned :class:`Solicitation` binds the eventual decision to
        ``(run_id, attempt_id, authority_seq, pending_handoff_ids)``. The Lead
        turn's decision must be applied through
        :meth:`resolve_lead_solicitation`, which is the single point that
        consumes it. Raises :class:`BudgetExhausted` (marking the run
        ``exhausted``) when no work slot remains.
        """
        exhausted = False
        result: Optional[Solicitation] = None
        with self._txn() as c:
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] != RunStatus.ACTIVE.value:
                raise DecisionError(
                    f"run {run_id} is {run['status']}; resume it before soliciting Lead"
                )
            if run["routing_authority"] != LEAD_PENDING:
                raise DecisionError(
                    f"routing authority is held by {run['routing_authority']}, not Lead; "
                    "the in-flight attempt must reach a terminal outcome first"
                )

            limit = self.limits.max_work
            if limit and run["reserved_slots"] >= limit:
                c.execute(
                    "UPDATE goal_run SET status = ? WHERE id = ?",
                    (RunStatus.EXHAUSTED.value, run_id),
                )
                exhausted = True
            else:
                dispatch = self._create_dispatch(
                    c, run_id,
                    LeadDecision(DecisionKind.SOLICIT_LEAD, role="lead", reason="solicit_lead"),
                )
                attempt_id = _new_id("att")
                c.execute(
                    "INSERT INTO attempt (id, dispatch_id, run_id, role, state, reserved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (attempt_id, dispatch.id, run_id, "lead", AttemptState.RESERVED.value, _now()),
                )
                c.execute(
                    "UPDATE goal_run SET reserved_slots = reserved_slots + 1, "
                    "routing_authority = ? WHERE id = ?",
                    (dispatch.id, run_id),
                )
                pend = c.execute(
                    "SELECT id FROM handoff WHERE run_id = ? AND consumed_by_dispatch_id IS NULL "
                    "ORDER BY created_at",
                    (run_id,),
                ).fetchall()
                pending_ids = tuple(r["id"] for r in pend)
                c.execute(
                    "INSERT INTO solicitation "
                    "(attempt_id, dispatch_id, run_id, authority_seq, pending_ids, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (attempt_id, dispatch.id, run_id, run["authority_seq"],
                     json.dumps(list(pending_ids)), _now()),
                )
                result = Solicitation(
                    attempt_id=attempt_id,
                    dispatch_id=dispatch.id,
                    run_id=run_id,
                    authority_seq=run["authority_seq"],
                    pending_handoff_ids=pending_ids,
                )
        # Raise only after the EXHAUSTED status is durably committed.
        if exhausted:
            raise BudgetExhausted(f"max_work={self.limits.max_work} reached for run {run_id}")
        assert result is not None
        return result

    def resolve_lead_solicitation(
        self,
        attempt_id: str,
        *,
        outcome: AttemptOutcome,
        decision: Optional[LeadDecision],
        configured_roles,
        gate_correction: Optional[str] = None,
    ) -> DecisionResult:
        """Consume a solicited Lead decision exactly once, atomically.

        ``decision`` is the *untrusted* candidate captured in attempt-local
        memory from the Lead turn (e.g. via the ``submit_lead_decision`` SDK
        tool); it is ``None`` when the turn produced no decision (timeout,
        error, cancel). This method terminalises the journaled Lead attempt
        (never emitting a handoff) and then either:

        * **applies** the decision — but only if the Lead turn completed cleanly,
          the authority nonce is unchanged, the decision acknowledges *exactly*
          the snapshot's pending handoffs, and any dispatch target is configured
          — resetting the invalid-solicitation counter; or
        * **records it invalid** — increments the invalid-solicitation counter
          and returns authority to Lead for another (budgeted) solicitation,
          persisting a ``paused`` blocker once
          ``max_invalid_solicitations`` is reached.

        Raises :class:`DecisionError` if ``attempt_id`` is not an in-flight
        solicitation (e.g. already reconciled by a restart — the stale candidate
        must be dropped, never applied under a later authority window).
        """
        configured = set(configured_roles)
        with self._txn() as c:
            att = c.execute("SELECT * FROM attempt WHERE id = ?", (attempt_id,)).fetchone()
            if att is None:
                raise KeyError(attempt_id)
            sol = c.execute(
                "SELECT * FROM solicitation WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if sol is None:
                raise DecisionError(f"attempt {attempt_id} is not a Lead solicitation")
            if att["state"] not in (AttemptState.RESERVED.value, AttemptState.STARTED.value):
                raise DecisionError(
                    f"solicitation {attempt_id} is not in-flight ({att['state']}); "
                    "its candidate decision is stale and must be dropped"
                )
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (sol["run_id"],)).fetchone()

            # Terminalise the journaled Lead attempt (no handoff emitted).
            c.execute(
                "UPDATE attempt SET state = ?, terminal_outcome = ?, terminal_at = ? WHERE id = ?",
                (AttemptState.TERMINAL.value, outcome.value, _now(), attempt_id),
            )

            # A candidate decision may only advance state — including being
            # downgraded to a typed gate correction (#64) — once the solicitation
            # is proven clean and valid for the *current* authority window: a
            # clean outcome, a well-formed non-solicit decision, an unchanged
            # authority nonce still owned by this solicit dispatch, acknowledgement
            # of exactly the snapshot's pending handoffs, and a configured dispatch
            # role. A failed, malformed, or stale solicitation takes the
            # invalid/stale path below and is NEVER downgraded to a benign
            # correction.
            snapshot = set(json.loads(sol["pending_ids"]))
            ok, reason = self._solicitation_valid(
                run, sol, outcome, decision, configured, snapshot
            )
            if ok:
                assert decision is not None
                # The Lead turn was well-formed and valid for this authority
                # window. A correctable, intent-aware gate rejection means the
                # public record does not yet satisfy the routed intent's predicate:
                # drop the candidate WITHOUT reserving an attempt, acking a handoff,
                # transferring authority, or rebinding task/PR identity, and return
                # a typed correction to Lead so it can repair the record or reroute.
                # Bounded: after ``max_gate_corrections`` consecutive corrections it
                # settles into a WAITING state (not a human PAUSE) so a persistently
                # inconsistent public record surfaces without a false human blocker.
                if gate_correction is not None:
                    return self._record_gate_correction(c, run, sol["run_id"], gate_correction)
                # A productive Lead decision clears the correctable-gate streak and
                # any stored correction so a later unrelated rejection starts fresh.
                c.execute(
                    "UPDATE goal_run SET invalid_solicitations = 0, "
                    "consecutive_corrections = 0, gate_correction = NULL WHERE id = ?",
                    (sol["run_id"],),
                )
                return self._apply_solicited_decision(c, sol["run_id"], decision)

            new_invalid = run["invalid_solicitations"] + 1
            cap = self.limits.max_invalid_solicitations
            if cap and new_invalid >= cap:
                blocker = f"{new_invalid} invalid/failed Lead solicitations (cap {cap}): {reason}"
                c.execute(
                    "UPDATE goal_run SET invalid_solicitations = ?, status = ?, "
                    "routing_authority = ?, human_blocker = ?, authority_seq = authority_seq + 1 "
                    "WHERE id = ?",
                    (new_invalid, RunStatus.PAUSED.value, LEAD_PENDING, blocker, sol["run_id"]),
                )
            else:
                c.execute(
                    "UPDATE goal_run SET invalid_solicitations = ?, routing_authority = ?, "
                    "authority_seq = authority_seq + 1 WHERE id = ?",
                    (new_invalid, LEAD_PENDING, sol["run_id"]),
                )
            fresh = c.execute("SELECT * FROM goal_run WHERE id = ?", (sol["run_id"],)).fetchone()
            return DecisionResult(
                run=_run_view(fresh),
                solicitation_invalid=True,
                invalid_reason=reason,
            )

    # ── internal helpers ─────────────────────────────────────────
    def _insert_handoff(self, c, run_id, attempt_id, role, cls: HandoffOutcome,
                        evidence, changed, remaining, reason_returned,
                        *, disagreement: str = "", blocker: str = "") -> str:
        handoff_id = _new_id("ho")
        c.execute(
            "INSERT INTO handoff (id, attempt_id, run_id, role, outcome_class, evidence, "
            "changed, remaining, reason_returned, disagreement, blocker, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (handoff_id, attempt_id, run_id, role, cls.value, evidence, changed,
             remaining, reason_returned, disagreement, blocker, _now()),
        )
        return handoff_id

    def _return_to_lead(self, c, run_id: str, cls: HandoffOutcome) -> None:
        """Hand routing authority back to Lead and update the unproductive counter."""
        run = c.execute("SELECT consecutive_unproductive FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        unproductive = (run["consecutive_unproductive"] + 1) if cls.is_unproductive else 0
        c.execute(
            "UPDATE goal_run SET routing_authority = ?, consecutive_unproductive = ?, "
            "authority_seq = authority_seq + 1 WHERE id = ?",
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

    def _record_gate_correction(
        self, c, run, run_id: str, correction: str
    ) -> DecisionResult:
        """Return a typed, bounded gate correction to Lead (#64).

        Keeps authority with Lead (``LEAD_PENDING``) and the run ``active`` while
        the streak is under ``max_gate_corrections`` — a later Lead turn can
        repair the public record or reroute — bumping the authority nonce so the
        dropped candidate can never apply under a stale window. When the streak
        reaches the cap the run settles into a bounded ``WAITING`` state with a
        wake condition (NOT a human ``PAUSED``): a correctable public-record
        inconsistency that merely repeats is not a human blocker (GOAL.md), so it
        surfaces as a recoverable wait that a later reconcile/resume re-evaluates
        rather than halting for an operator. The correctable rejection is
        deliberately NOT counted as an invalid Lead solicitation.
        """
        streak = (run["consecutive_corrections"] or 0) + 1
        cap = self.limits.max_gate_corrections
        if cap and streak >= cap:
            wake = (
                f"{streak} consecutive uncorrected gate rejections (cap {cap}); "
                f"latest correction: {correction}"
            )
            c.execute(
                "UPDATE goal_run SET consecutive_corrections = ?, gate_correction = ?, "
                "status = ?, routing_authority = ?, wake_condition = ?, "
                "authority_seq = authority_seq + 1 WHERE id = ?",
                (streak, correction, RunStatus.WAITING.value, LEAD_PENDING, wake, run_id),
            )
        else:
            c.execute(
                "UPDATE goal_run SET consecutive_corrections = ?, gate_correction = ?, "
                "routing_authority = ?, authority_seq = authority_seq + 1 WHERE id = ?",
                (streak, correction, LEAD_PENDING, run_id),
            )
        fresh = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        return DecisionResult(
            run=_run_view(fresh),
            gate_corrected=True,
            gate_correction_reason=correction,
        )

    def latest_gate_correction(self, run_id: str) -> Optional[str]:
        """The most recent typed gate correction returned to Lead, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT gate_correction FROM goal_run WHERE id = ?", (run_id,)
            ).fetchone()
        return row["gate_correction"] if row is not None else None

    def _synthetic_pause(self, c, run_id: str, blocker: str) -> None:
        """Emit one synthetic handoff and pause, rather than looping into Lead forever.

        The synthetic handoff represents non-Lead work/control evidence, so it is
        attached to the most recent *non-solicitation* attempt. A ``SOLICIT_LEAD``
        attempt must never produce a handoff through any kernel path, so if the
        only recent attempt is a Lead solicitation (e.g. the guard trips while
        applying a solicited ``continue_lead``/``dispatch`` decision), the run
        pauses without inserting a synthetic ``role='lead'`` handoff.
        """
        att = c.execute(
            "SELECT a.id, a.role FROM attempt a "
            "WHERE a.run_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM solicitation s WHERE s.attempt_id = a.id) "
            "ORDER BY a.reserved_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if att is not None:
            self._insert_handoff(
                c, run_id, att["id"], att["role"], HandoffOutcome.UNCERTAIN,
                evidence="", changed="", remaining=blocker, reason_returned="thrash_guard",
            )
        c.execute(
            "UPDATE goal_run SET status = ?, routing_authority = ?, human_blocker = ?, "
            "authority_seq = authority_seq + 1 WHERE id = ?",
            (RunStatus.PAUSED.value, LEAD_PENDING, blocker, run_id),
        )

    def _create_dispatch(self, c, run_id: str, decision: LeadDecision) -> DispatchView:
        seq_row = c.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM dispatch WHERE run_id = ?", (run_id,)
        ).fetchone()
        seq = seq_row["m"] + 1
        dispatch_id = _new_id("dsp")
        c.execute(
            "INSERT INTO dispatch "
            "(id, run_id, seq, kind, role, reason, task_number, pr_number, intent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (dispatch_id, run_id, seq, decision.kind.value, decision.role,
             decision.reason, decision.task_number, None,
             (decision.intent.value if decision.intent else None), _now()),
        )
        row = c.execute("SELECT * FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()
        return _dispatch_view(row)

    def _solicitation_valid(
        self, run, sol, outcome, decision, configured, snapshot,
    ) -> tuple[bool, Optional[str]]:
        """Whether a solicited candidate decision may be applied. Pure predicate."""
        if outcome is not AttemptOutcome.IDLE_COMPLETED:
            return False, f"Lead turn did not complete cleanly (outcome={outcome.value})"
        if decision is None:
            return False, "Lead turn submitted no decision"
        if decision.kind is DecisionKind.SOLICIT_LEAD:
            return False, "solicit_lead is not a valid Lead decision"
        # Authority nonce must be unchanged since the solicitation was opened, and
        # the solicit dispatch must still own routing authority. A restart that
        # reconciled this attempt would have bumped the nonce and returned
        # authority to Lead, so a late candidate can never apply.
        if run["authority_seq"] != sol["authority_seq"]:
            return False, "authority window changed since the solicitation was opened"
        if run["routing_authority"] != sol["dispatch_id"]:
            return False, "solicit dispatch no longer owns routing authority"
        if set(decision.ack_handoff_ids) != snapshot:
            return False, "decision must acknowledge exactly the snapshot's pending handoffs"
        if decision.kind is DecisionKind.DISPATCH and (
            not decision.role or decision.role not in configured
        ):
            return False, f"dispatch target {decision.role!r} is not a configured role"
        return True, None

    def _apply_solicited_decision(self, c, run_id: str, decision: LeadDecision) -> DecisionResult:
        """Apply a validated solicited decision inside the open transaction.

        Differs from :meth:`lead_decide` only in that ``CONTINUE_LEAD`` returns
        authority to Lead (which drives another budgeted solicitation) rather
        than granting a free recursive Lead turn.
        """
        run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        self._validate_acks(c, run_id, decision.ack_handoff_ids)

        routing = decision.kind in (DecisionKind.DISPATCH, DecisionKind.CONTINUE_LEAD)
        if routing:
            edge = decision.role or "lead"
            tripped, reason = self._thrash_reason(run, edge)
            if tripped:
                self._synthetic_pause(c, run_id, reason)
                fresh = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
                return DecisionResult(run=_run_view(fresh), guard_tripped=True, guard_reason=reason)

        dispatch = self._create_dispatch(c, run_id, decision)
        self._apply_acks(c, decision.ack_handoff_ids, dispatch.id)

        if decision.kind is DecisionKind.DISPATCH:
            edge = decision.role or "lead"
            repeats = (run["last_edge_repeats"] + 1) if run["last_edge"] == edge else 1
            c.execute(
                "UPDATE goal_run SET routing_authority = ?, last_edge = ?, last_edge_repeats = ? "
                "WHERE id = ?",
                (dispatch.id, edge, repeats, run_id),
            )
        elif decision.kind is DecisionKind.CONTINUE_LEAD:
            # No free recursion: authority returns to Lead so the orchestrator
            # opens another budgeted solicitation.
            edge = "lead"
            repeats = (run["last_edge_repeats"] + 1) if run["last_edge"] == edge else 1
            c.execute(
                "UPDATE goal_run SET routing_authority = ?, last_edge = ?, last_edge_repeats = ?, "
                "authority_seq = authority_seq + 1 WHERE id = ?",
                (LEAD_PENDING, edge, repeats, run_id),
            )
        else:
            status = {
                DecisionKind.WAIT: RunStatus.WAITING,
                DecisionKind.PAUSE: RunStatus.PAUSED,
                DecisionKind.FINISH: RunStatus.FINISHED,
            }[decision.kind]
            c.execute(
                "UPDATE goal_run SET status = ?, routing_authority = ?, wake_condition = ?, "
                "human_blocker = ?, authority_seq = authority_seq + 1 WHERE id = ?",
                (status.value, LEAD_PENDING, decision.wake_condition, decision.human_blocker, run_id),
            )
        fresh = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        return DecisionResult(run=_run_view(fresh), dispatch=dispatch)

    def mark_run_status(
        self, run_id: str, status: RunStatus, *,
        human_blocker: str | None = None, wake_condition: str | None = None,
    ) -> RunView:
        """Durably record an orchestrator-driven halt.

        This is the write counterpart to :meth:`resume_run`: it lets the
        orchestrator persist *why* a run stopped launching work so a restart sees
        the halt instead of resuming as if nothing happened. The operator halt
        states (stopped/paused/interrupted) record a ``human_blocker``; a
        self-healing ``waiting`` records a ``wake_condition`` instead — a recoverable
        public-write ordering/outage condition that the next run reconciles without
        operator action (issue #49), as opposed to a ``paused`` human blocker.
        Routing decisions (the Lead ``wait``/``finish`` path) still flow through
        :meth:`lead_decide` / :meth:`resolve_lead_solicitation`, and ``exhausted``
        is set only by the budget guards. Idempotent: re-marking a run already in
        the target status is a no-op. Terminal ``finished`` / ``exhausted`` runs
        cannot be overwritten (raises :class:`DecisionError`).
        """
        allowed = {
            RunStatus.STOPPED, RunStatus.PAUSED, RunStatus.INTERRUPTED,
            RunStatus.WAITING,
        }
        if status not in allowed:
            raise DecisionError(f"mark_run_status only accepts {allowed}, not {status}")
        terminal = {RunStatus.FINISHED.value, RunStatus.EXHAUSTED.value}
        with self._txn() as c:
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] == status.value:
                return _run_view(run)
            if run["status"] in terminal:
                raise DecisionError(
                    f"run {run_id} is {run['status']} (terminal) and cannot be {status.value}"
                )
            c.execute(
                "UPDATE goal_run SET status = ?, human_blocker = ?, wake_condition = ? WHERE id = ?",
                (status.value, human_blocker, wake_condition, run_id),
            )
            run = c.execute("SELECT * FROM goal_run WHERE id = ?", (run_id,)).fetchone()
        return _run_view(run)

    # ── read helpers (test/observability) ────────────────────────
    def get_attempt(self, attempt_id: str) -> AttemptView:
        row = self._conn.execute("SELECT * FROM attempt WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return _attempt_view(row)

    def get_dispatch(self, dispatch_id: str) -> DispatchView:
        row = self._conn.execute("SELECT * FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()
        if row is None:
            raise KeyError(dispatch_id)
        return _dispatch_view(row)

    def bind_pr_to_dispatch(self, dispatch_id: str, pr_number: int) -> DispatchView:
        """Persist the immutable PR proven by the Verifier routing gate."""
        with self._txn() as c:
            row = c.execute(
                "SELECT * FROM dispatch WHERE id = ?", (dispatch_id,)
            ).fetchone()
            if row is None:
                raise KeyError(dispatch_id)
            existing = row["pr_number"]
            if existing is not None and int(existing) != int(pr_number):
                raise DecisionError(
                    f"dispatch {dispatch_id} already bound to PR #{existing}, "
                    f"cannot rebind to PR #{pr_number}"
                )
            if existing is None:
                c.execute(
                    "UPDATE dispatch SET pr_number = ? WHERE id = ?",
                    (int(pr_number), dispatch_id),
                )
            row = c.execute(
                "SELECT * FROM dispatch WHERE id = ?", (dispatch_id,)
            ).fetchone()
        return _dispatch_view(row)

    def binding_for_handoff(
        self, handoff_id: str
    ) -> tuple[Optional[int], Optional[int]]:
        """Return the persisted task/PR binding for a handoff's dispatch."""
        row = self._conn.execute(
            "SELECT d.task_number AS tn, d.pr_number AS pn FROM handoff h "
            "JOIN attempt a ON a.id = h.attempt_id "
            "JOIN dispatch d ON d.id = a.dispatch_id "
            "WHERE h.id = ?",
            (handoff_id,),
        ).fetchone()
        if row is None:
            return None, None
        return row["tn"], row["pn"]

    def task_number_for_handoff(self, handoff_id: str) -> Optional[int]:
        """The exact routed task bound to a handoff's originating dispatch.

        Resolves handoff → attempt → dispatch and returns the persisted
        ``task_number``. This is the durable binding the public writer uses so a
        role handoff is published to the *routed* task, never a later global
        re-census of the public record (issue #47). Returns ``None`` for a
        handoff whose dispatch carries no task (e.g. a synthetic guard handoff)
        or an unknown id, so the caller surfaces a recoverable route rather than
        guessing.
        """
        return self.binding_for_handoff(handoff_id)[0]

    def read_run_diagnostics(self, goal_label: str) -> "Optional[RunDiagnostics]":
        """Read-only, point-in-time projection of the latest run for a goal label.

        Performed in a **single deferred read transaction** so the run, its
        in-flight attempt, latest handoff and latest Lead decision are all
        observed at one consistent snapshot (they cannot shift mid-read while a
        turn terminalises). Returns ``None`` when the label has no run yet.

        This never mutates — unlike :meth:`start_or_resume_run`, it does **not**
        create a run for an unseen label. It is the durable-truth source for the
        operator diagnostic surface; control/liveness/session facts are layered
        on top (at explicitly lower authority) by :mod:`crewd.diagnostics`.

        ``current_attempt`` is the attempt currently holding routing authority
        (``routing_authority`` is a dispatch id, not ``lead_pending``); after a
        crash this is exactly the orphaned ``started`` attempt awaiting
        restart reconciliation.
        """
        with self._txn() as c:
            run_row = c.execute(
                "SELECT * FROM goal_run WHERE goal_label = ? ORDER BY created_at DESC LIMIT 1",
                (goal_label,),
            ).fetchone()
            if run_row is None:
                return None
            run = _run_view(run_row)

            current_attempt: Optional[AttemptView] = None
            if run.routing_authority != LEAD_PENDING:
                att_row = c.execute(
                    "SELECT * FROM attempt WHERE dispatch_id = ? AND run_id = ? "
                    "AND state IN (?, ?)",
                    (run.routing_authority, run.id,
                     AttemptState.RESERVED.value, AttemptState.STARTED.value),
                ).fetchone()
                if att_row is not None:
                    current_attempt = _attempt_view(att_row)

            ho_row = c.execute(
                "SELECT * FROM handoff WHERE run_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (run.id,),
            ).fetchone()
            latest_handoff = _handoff_view(ho_row) if ho_row is not None else None

            pending = c.execute(
                "SELECT COUNT(*) AS n FROM handoff WHERE run_id = ? "
                "AND consumed_by_dispatch_id IS NULL",
                (run.id,),
            ).fetchone()["n"]

            dsp_row = c.execute(
                "SELECT * FROM dispatch WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
                (run.id,),
            ).fetchone()
            latest_dispatch = _dispatch_view(dsp_row) if dsp_row is not None else None

        return RunDiagnostics(
            run=run,
            current_attempt=current_attempt,
            latest_handoff=latest_handoff,
            pending_handoff_count=int(pending),
            latest_dispatch=latest_dispatch,
        )

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
        authority_seq=row["authority_seq"],
        invalid_solicitations=row["invalid_solicitations"],
        consecutive_corrections=(
            row["consecutive_corrections"]
            if "consecutive_corrections" in row.keys() else 0
        ),
        gate_correction=(
            row["gate_correction"] if "gate_correction" in row.keys() else None
        ),
    )


def _dispatch_view(row: sqlite3.Row) -> DispatchView:
    keys = row.keys()
    task_number = row["task_number"] if "task_number" in keys else None
    pr_number = row["pr_number"] if "pr_number" in keys else None
    intent_raw = row["intent"] if "intent" in keys else None
    # A DISPATCH always carries an intent (legacy/untyped → IMPLEMENTATION); a
    # non-dispatch audit row (wait/pause/finish/continue/solicit) has none.
    kind = DecisionKind(row["kind"])
    intent = (
        DispatchIntent.coerce(intent_raw)
        if kind is DecisionKind.DISPATCH
        else (DispatchIntent.coerce(intent_raw) if intent_raw else None)
    )
    return DispatchView(
        id=row["id"], run_id=row["run_id"], seq=row["seq"], kind=kind,
        role=row["role"], reason=row["reason"], created_at=row["created_at"],
        task_number=task_number,
        pr_number=pr_number,
        intent=intent,
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
        disagreement=row["disagreement"], blocker=row["blocker"],
        created_at=row["created_at"], consumed_by_dispatch_id=row["consumed_by_dispatch_id"],
    )
