# Orchestrator — dispatcher-driven run loop

The orchestrator (`src/crewd/orchestrator.py`) replaces the fixed round-robin
scheduler that walked `for role in ROLES` every cycle. It drives the durable
solicitation kernel (`docs/dispatcher.md`) so that **Lead decides what runs
next**, the decision is consumed exactly once, and each chosen attempt (a role
tick or another Lead turn) executes through the typed `AttemptExecutor` seam
(`src/crewd/executor.py`).

## Pieces

| Module | Responsibility |
|--------|----------------|
| `dispatcher.py` | Durable kernel: reserves work slots, journals Lead solicitations/decisions, records terminal outcomes, reconciles on restart. Knows nothing about *how* an attempt runs. |
| `executor.py` | Typed seam. `AttemptRequest` (all inputs), `RoleAttemptOutcome` / `LeadTurnOutcome` (an `AttemptResult` + durable session identity + — for Lead — the untrusted candidate `LeadDecision`), the `AttemptExecutor` protocol, and `SdkAttemptExecutor` (production, over `sdk_adapter`). |
| `orchestrator.py` | Drives kernel + executor to a terminal condition, maps each terminal condition to a persisted exit reason. |
| `commands.py` | `build_executor(cfg)` selects the executor; `cmd_run` / `cmd_run_daemon` build and run the `Orchestrator`, installing signal handlers. |

## One `crewd run` invocation

`Orchestrator.run(once)` advances the goal run until it leaves `active`:

1. `start_or_resume_run`; if the run is resumable (paused/waiting/interrupted/
   stopped) `resume_run` revives it — an explicit `crewd run` is intent to make
   progress, mirroring the workspace-sentinel clear the command layer performs.
2. `reconcile_on_restart` recovers any attempt orphaned by a crash **before**
   new work (never replays it; bumps the solicitation nonce so a lost in-memory
   Lead candidate can never be applied).
3. Loop: `_check_controls` (interrupt/stop/pause → persist status + exit reason)
   → if run status ≠ `active` return the mapped exit reason → `_step`.

`--once` runs exactly one `_step`, then reports a terminal reason if that step
reached one.

### Status → exit reason

| RunStatus | exit reason |
|-----------|-------------|
| FINISHED / STOPPED | `goal-complete` |
| PAUSED | `human-blocked` |
| WAITING | `waiting` |
| EXHAUSTED | `exhausted` |
| INTERRUPTED | `interrupted` |

## A step

`_step` reads the run's routing authority:

- **Lead pending** → `_lead_step`: `open_lead_solicitation` (reserves one
  budgeted Lead attempt that takes authority; `BudgetExhausted` → the run is
  marked exhausted and the loop exits) → build the Lead prompt embedding the
  exact pending-handoff ids → `run_lead` → `mark_started` (persist Lead session
  identity) → `resolve_lead_solicitation` (the *single* transaction that decides
  whether to apply the candidate, under all semantic guards).
- **Dispatched to a role** → `_dispatch_step`: `get_dispatch` → role →
  `reserve_attempt` → `execute_role` → `mark_started` → `record_terminal`
  (`reason_returned="sdk:<outcome>"`).

The orchestrator consumes `AttemptResult.outcome` directly — it never
reconstructs lifecycle meaning from a process exit code. That reversal of the
#10 exit-code mapping is the point of the executor seam.

## Signals / operator controls

Signal handlers (`request_stop`) set an interrupted flag; the loop checks it
**between** steps and exits `interrupted` after persisting `RunStatus.INTERRUPTED`.
A second signal exits hard (130). Workspace `STOPPED` / `PAUSED` sentinels are
honoured the same way.

## Deferred (scoped follow-up)

Mid-attempt cancellation (`request_cancel(reason)` + a distinct clean-cancel
terminal in `run_attempt`) and taint-before-finalize orphan-session recovery are
a separate slice. A POSIX signal handler runs re-entrantly on the main thread
while it is blocked inside the async SDK bridge, so safe *in-flight* cancellation
requires a worker-thread execution model — genuinely independent race-handling
work kept out of this PR for reviewability. Today an in-flight attempt
interrupted by a signal is reconciled `uncertain` on the next start and never
replayed; its SDK session is left resumable rather than force-tainted.

## Tests

`tests/test_orchestrator.py` is the end-to-end fake-SDK matrix (`tests/fakes.py`
provides a scriptable `FakeExecutor`): happy-path dispatch→finish, pause, wait,
budget exhaustion, role failure, invalid solicitations, `once=True`, stop
sentinel, advisory dispatch, `continue_lead`, restart reconciliation, thrash
guard, and signal interrupt. The command-layer loop tests
(`tests/test_commands.py`, `tests/test_goal_lifecycle.py`) drive `cmd_run` with a
monkeypatched `build_executor` returning a `FakeExecutor`, so no test touches the
real SDK.
