# Durable dispatch kernel (issue #11, slice A)

Status: **kernel landed, not yet wired into the loop.** This slice introduces
`crewd.dispatcher` and its tests only. Replacing the round-robin
`_LoopController`, routing real SDK attempts, and rejecting the legacy
`backend: copilot` transport are **slice B** (a separate issue) — they are where
operator-visible behavior changes.

## Why a kernel, and why SQLite

The fixed round-robin loop persisted only a cycle counter and observed
STOPPED/PAUSED sentinels between synchronous role calls. That is not a
crash-consistent oracle for a Lead-directed dispatcher: several facts must be
updated together and survive a crash at any point.

The kernel uses **one stdlib SQLite database, one writer**. Each mutating method
runs as a single `BEGIN IMMEDIATE … COMMIT` transaction, so SQLite's atomic
commit gives all-or-nothing durability across crash/power loss. No multi-file
reconciliation protocol; the rollback journal is sufficient for a single writer.

`PRAGMA synchronous = FULL` and `foreign_keys = ON` are set. The SDK *session*
state (owned by `session_backend` / `sdk_adapter`) stays separate from this
*orchestration* state.

## State model

| table | meaning |
|-------|---------|
| `goal_run` | one row per goal epoch; holds status, `routing_authority`, durable `reserved_slots`, thrash counters, wake condition / human blocker |
| `dispatch` | one row per Lead decision (every kind, for audit + as the acknowledgement anchor) |
| `attempt` | a reserved → started → terminal (or reconciled-uncertain) role attempt |
| `handoff` | immutable result of an attempt; consumed only by an acknowledging dispatch |

`routing_authority` is `lead_pending` when Lead must decide next, or the id of
the in-flight dispatch otherwise.

## Invariants (frozen by tests)

1. **Typed outcomes only.** Terminal attempt outcomes are
   `session_backend.AttemptOutcome`; the routing-level `HandoffOutcome` class is
   explicit. Nothing is inferred from exit codes. `.attempt.json` remains a
   derived export.
2. **Exactly one terminal per attempt.** A second `record_terminal` raises.
3. **Durable pre-invocation reservation.** `reserve_attempt` counts a slot
   *before* the executor runs; a crash/restart never refunds it. Exceeding
   `max_work` durably marks the run `exhausted` (committed before the raise).
4. **At-least-once handoffs, idempotent acknowledgement.** A handoff is consumed
   only by Lead's next structured decision naming its id — never because it was
   put in a prompt. Re-acknowledging a consumed handoff is a no-op; acking an
   unknown id rolls the whole decision back (all-or-nothing).
5. **Restart reconciliation, never auto-replay.** `reconcile_on_restart` marks
   any `reserved`/`started` attempt with no terminal record as
   `reconciled_uncertain`, emits one `uncertain` handoff, and returns authority
   to Lead. Idempotent.
6. **Closed Lead-decision sum type.** `dispatch(role)`, `continue_lead`,
   `wait(condition)`, `pause(blocker)`, `finish(acceptance)`. A `dispatch` to an
   unconfigured role fails and keeps authority with Lead. `wait`/`pause`/`finish`
   launch no role.
7. **Exclusive routing authority.** `lead_decide` is accepted only when the run
   is `active` **and** `routing_authority == lead_pending`. This forbids
   overlapping dispatches while one attempt is in flight, and refuses to launch
   work from a `paused`/`finished`/`exhausted`/`interrupted` run. Reviving a
   non-active run requires the explicit `resume_run` transition; a plain
   restart (`start_or_resume_run`) returns the existing durable run **as-is**
   and never creates a second active run that bypasses a persisted pause/finish.
8. **Attempts are bound to their dispatch.** `reserve_attempt` requires the
   dispatch to exist, belong to the run, be a routing kind, match the role, and
   currently own routing authority; at most one attempt per dispatch
   (`UNIQUE(dispatch_id)` backstop). A stale caller after a crash therefore
   cannot launch a second role while the journal says another dispatch owns
   authority, nor bind an attempt to the wrong run/role.
9. **Deterministic thrash bounds.** Exceeding `max_consecutive_unproductive`, or
   repeating an identical role edge more than `max_edge_repeats` times without an
   intervening productive handoff, emits one synthetic handoff and **pauses** the
   run (`guard_tripped=True`) instead of recursively invoking Lead. `resume_run`
   clears the blocker and resets the thrash counters. Thresholds are configurable
   pending #12's semantic progress token.
10. **Journaled Lead solicitation (#17).** A Lead *decision-production* SDK call is
    real Premium work, so it is durably reserved and bounded exactly like role
    work — never an unjournaled orchestration call. `open_lead_solicitation(run)`
    creates a `solicit_lead` dispatch that takes routing authority and owns
    **one** Lead attempt (consuming a `max_work` slot), and snapshots the current
    authority nonce (`goal_run.authority_seq`) plus the *exact* set of pending
    handoff ids Lead is asked to route. Unlike role attempts, a solicitation
    attempt **suppresses handoff emission** (only non-Lead completions emit
    handoffs), including on restart reconciliation. The Lead turn's decision is an
    untrusted candidate captured in attempt-local memory (e.g. via the
    `submit_lead_decision` SDK tool); it is consumed **exactly once** through
    `resolve_lead_solicitation`, which in a single transaction terminalizes the
    Lead attempt and then either **applies** the decision — only if the turn
    completed cleanly, the authority nonce is unchanged, and the decision
    acknowledges *exactly* the snapshot set — or **records it invalid**. Invalid /
    missing / timed-out / errored decisions increment
    `goal_run.invalid_solicitations` and return authority to Lead for another
    bounded solicitation; reaching `max_invalid_solicitations` persists a
    `paused` blocker. `continue_lead` returns authority to Lead (another budgeted
    solicitation), never a free recursive call. The authority nonce is bumped on
    every transition back to `lead_pending`, so a candidate produced under an
    earlier authority window (e.g. one whose attempt a restart already reconciled
    to `uncertain`) can never apply — `resolve_lead_solicitation` refuses a
    solicitation attempt that is no longer in-flight. SQLite, not any
    `decision.json` file, owns idempotent consumption. A solicitation attempt is
    terminalized **only** through `resolve_lead_solicitation`: `record_terminal`
    rejects it, and the thrash/no-progress guard reached while applying a
    solicited decision pauses **without** synthesising a `role='lead'` handoff
    (the synthetic handoff attaches only to the most recent non-solicitation
    attempt, i.e. real role work/control evidence). So no kernel path — public or
    internal — can ever create a Lead handoff.

## Schema migration

The kernel opens a database created by an earlier version in place. `CREATE
TABLE IF NOT EXISTS` creates any *missing* tables (e.g. the `solicitation` table
and its indexes on a pre-#17 database) but never alters an existing `goal_run`,
so `Dispatcher.__init__` runs an idempotent `_migrate()` that adds the
`authority_seq` / `invalid_solicitations` columns to `goal_run` and the #12
`disagreement` / `blocker` columns to `handoff` (all `NOT NULL DEFAULT`) when
absent, inside one transaction, and stamps `PRAGMA user_version`. The
per-table column-existence check is the durable oracle (safe across repeated
opens); `user_version` is a derived marker. Existing runs/attempts/handoffs are
preserved; the real upgrade path is covered by
`tests/test_dispatcher.py` (`test_migration_*`).

## Crash points covered by `tests/test_dispatcher.py`

A "crash" discards the `Dispatcher` and reopens a fresh one on the same file:

- before reserve — nothing persisted;
- after reserve / before start — attempt `reserved`, slot not refunded → reconcile to uncertain;
- in-flight (`started`, no terminal) → reconcile to uncertain;
- after terminal — handoff durable and pending until acked;
- before Lead ack — handoff still deliverable;
- after Lead ack — consumed; re-ack idempotent;
- Lead solicitation in-flight (crash between the Lead turn and resolution) →
  reconciled `uncertain` with **no** handoff emitted, authority returns to Lead
  with a bumped nonce, and the lost in-memory candidate can never be applied.

## Integration status (issue #17)

The kernel is now wired into production via `orchestrator.py` and the typed
`AttemptExecutor` seam — see `docs/orchestrator.md`. Delivered:

- `orchestrator.py` replaces the round-robin loop; drives
  `open_lead_solicitation → run Lead turn → resolve_lead_solicitation` and
  `reserve → start → record_terminal` through the executor seam.
- Typed `AttemptExecutor` / `SdkAttemptExecutor` / `FakeExecutor` seam
  (`executor.py`, `tests/fakes.py`) plus the `submit_lead_decision` custom tool
  (built with the official `copilot.define_tool` API) whose handler only captures
  a candidate in an attempt-local, thread-safe `LeadDecisionCapture`. The capture
  enforces exactly-one submission: a sequential or concurrent second call makes
  the solicitation invalid rather than overwriting into an apparently valid
  candidate. Session identity is journaled (`mark_started`) **before** any SDK
  send.
- `backend: copilot` rejected with a migration diagnostic; `CopilotBackend`
  deleted.
- Full fake-SDK routing/restart matrix (`tests/test_orchestrator.py`).
- Thread-safe in-flight external cancellation: a single `CancelToken`
  (`session_backend.py`) shared by timeout / signal / operator-stop requests a
  **non-blocking** SDK abort while the attempt runs on a worker thread; the
  `run_attempt` state machine remains the sole abort/escalation owner. An
  externally cancelled turn that settles idle is the distinct
  `AttemptOutcome.CANCELLED_CLEAN` (handoff class `cancelled`), never a
  completion; an unconfirmed abort taints. `reconcile_on_restart(taint_orphan=…)`
  taints an orphaned `started` session **before** finalizing its uncertain
  handoff, so a crashed generation is never resumed normally; the taint is
  idempotent and the finalize atomic, so recovery is itself durable/retryable.

## Deferred (clearly-scoped follow-up)

- Finalize `max_consecutive_unproductive` / `max_edge_repeats` once #12 defines
  the progress token that distinguishes real progress from a bare completion.
