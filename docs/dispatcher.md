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

## Crash points covered by `tests/test_dispatcher.py`

A "crash" discards the `Dispatcher` and reopens a fresh one on the same file:

- before reserve — nothing persisted;
- after reserve / before start — attempt `reserved`, slot not refunded → reconcile to uncertain;
- in-flight (`started`, no terminal) → reconcile to uncertain;
- after terminal — handoff durable and pending until acked;
- before Lead ack — handoff still deliverable;
- after Lead ack — consumed; re-ack idempotent.

## Open items (slice B / live-smoke)

- Wire the kernel into `commands.py` in place of the round-robin loop; feed real
  SDK attempts through `reserve → start → terminal`.
- Reject `backend: copilot` with an actionable migration diagnostic; prove by
  code search + test that no production `copilot -p` path remains.
- Finalize `max_consecutive_unproductive` / `max_edge_repeats` once #12 defines
  the progress token that distinguishes real progress from a bare completion.
