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
| FINISHED | `goal-complete` |
| STOPPED | `stopped` |
| PAUSED | `human-blocked` |
| WAITING | `waiting` |
| EXHAUSTED | `exhausted` |
| INTERRUPTED | `interrupted` |

Operator **stop** is deliberately distinct from goal completion: a stop halts a
run with no final acceptance, so it must never report `goal-complete`.

A non-active run holds a durable state (a Lead human blocker, a wait condition,
an interrupt, an operator stop, or a terminal finished/exhausted). A plain
`crewd run` **must not** erase it — reviving a paused/waiting/interrupted/stopped
run is the explicit `crewd resume` workflow's job only (`Orchestrator.run(...,
resume=True)` + `Dispatcher.resume_run`). This closes the durable-pause bypass
fixed in PR #16: a plain run can no longer silently clear a human blocker.

## A step

`_step` reads the run's routing authority:

- **Lead pending** → `_lead_step`: `open_lead_solicitation` (reserves one
  budgeted Lead attempt that takes authority; `BudgetExhausted` → the run is
  marked exhausted and the loop exits) → build the Lead prompt embedding the
  exact pending-handoff ids → `run_lead` (journaling `mark_started` **before** any
  SDK send, via the executor's `on_started` hook) → `resolve_lead_solicitation`
  (the *single* transaction that decides whether to apply the candidate, under
  all semantic guards).
- **Dispatched to a role** → `_dispatch_step`: `get_dispatch` → role →
  `reserve_attempt` → `execute_role` (journaling `mark_started` **before** the SDK
  send) → `record_terminal` (`reason_returned="sdk:<outcome>"`).

**Pre-send journaling.** The executor's `on_started(session_id, generation)` hook
is invoked after session selection but before any SDK send, so an attempt that
reaches the transport is always durably `started` with its session identity. This
is what makes the deferred taint/orphan-recovery follow-up *safe*. Journaling
failure is **not** swallowed — it propagates and aborts the run rather than
continuing with an unjournaled in-flight attempt.

The orchestrator consumes `AttemptResult.outcome` directly — it never
reconstructs lifecycle meaning from a process exit code. That reversal of the
#10 exit-code mapping is the point of the executor seam.

## Lead decision channel (`submit_lead_decision`)

Lead delivers its routing decision through a narrow SDK custom tool built with
the **official** `copilot.define_tool(...)` API and passed via the
`tools=[...]` `create_session` parameter (`sdk_adapter.make_lead_decision_tool`).
Registration failure is surfaced (an `SdkError` failing the Lead turn), never
swallowed — a signature drift can't silently drop the only decision channel.

The tool handler only records the untrusted candidate into an attempt-local,
thread-safe `LeadDecisionCapture` (`executor.py`); it never mutates durable
state. The capture enforces the accepted **exactly-one-submission** invariant:
it retains a submission count + the first payload, so a sequential *or* concurrent
second call makes the solicitation invalid (`result()` → `None`) rather than
overwriting into an apparently valid single candidate. `run_lead` returns a
candidate only when `count == 1`; `count == 0` or `count > 1` resolves as an
invalid solicitation with handoffs retained.


## Role handoff channel (`submit_role_handoff`) (#12)

Non-Lead roles return structured evidence to Lead through a second narrow SDK
custom tool, `submit_role_handoff` (`sdk_adapter.make_role_handoff_tool`), built
with the same official `copilot.define_tool(...)` API and reusing the shipped
**exactly-one-submission** capture discipline (`RoleHandoffCapture`, a
`_SingleSubmitCapture` sibling of `LeadDecisionCapture`). The handler records an
untrusted candidate `{outcome_class, evidence, changed, remaining, reason,
disagreement}`; it never mutates durable state. Zero, multiple, or malformed
submissions make the payload invalid rather than silently picking one.

**Transport lifecycle is authoritative.** `resolve_role_terminal` in
`executor.py` decides the durable outcome, not the role's self-report: if the SDK
lifecycle outcome is anything other than a clean `idle_completed`, that outcome
classifies the terminal (`reason_returned="sdk:<value>"`) and a role can never
upgrade a failed/cancelled/errored turn to `completed`. Only on a clean idle with
**exactly one** well-formed submission does the role's own `completed` /
`no_progress` class stand. Zero/multiple/malformed on a clean idle resolves to
`HandoffOutcome.UNCERTAIN` (`reason_returned` prefixed
`role_protocol_failure:`), which is unproductive and so counts toward the
no-progress thrash bound. The evidence/changed/remaining fields are threaded
through `record_terminal` into the dispatch journal regardless, and rendered
verbatim into Lead's next solicitation prompt so routing is grounded in the
role's actual report. `disagreement` is carried as evidence inside the reason —
never as routing authority.

## Signals / operator controls & in-flight cancellation

Each attempt runs on a short-lived **worker thread** while the main loop polls
operator controls. A pending interrupt (`request_stop` sets a flag) or a
workspace `STOPPED` sentinel is observed *while the attempt is in flight* and
trips a single `CancelToken`, which issues a **non-blocking** SDK abort so the
in-flight turn unwinds promptly instead of after the wait bound. A wait-timeout,
a signal, and an operator stop therefore all funnel through **one** cancellation
requester, while the `run_attempt` state machine remains the sole *owner* of the
abort → confirm → force-stop → taint escalation — there is never a double abort.

An externally cancelled turn that settles idle is the distinct
`AttemptOutcome.CANCELLED_CLEAN` (handoff class `cancelled`), **never**
`idle_completed`; an unconfirmed abort force-stops and taints. After the current
step terminalises, the between-steps control check maps the run to its durable
exit reason (`interrupted` / `stopped` / `human-blocked`). A second signal exits
hard (130). `PAUSED` is honoured between steps (a human blocker is not a reason
to abort in-flight work).

## Restart recovery (taint-before-finalize)

`reconcile_on_restart` runs before any new work. Every attempt still `started`
(its session identity already journaled **before** the SDK send) has its orphaned
session generation **tainted before** the uncertain handoff is finalized, so a
crashed in-flight generation is never resumed normally — the next session
decision refuses the tainted id and advances to a fresh recovery generation.
Because the taint is idempotent and the reconciling state transition is a single
atomic step, a crash *during* recovery is safe: re-running re-taints (a no-op)
and then finalizes, so recovery is itself durable and retryable. A hard second
signal or restart therefore cannot resume the orphan generation.

## Tests

`tests/test_orchestrator.py` is the end-to-end fake-SDK matrix (`tests/fakes.py`
provides a scriptable `FakeExecutor`): happy-path dispatch→finish, pause, wait,
budget exhaustion, role failure, invalid solicitations, `once=True`, stop
sentinel, advisory dispatch, `continue_lead`, restart reconciliation, thrash
guard, and signal interrupt. The command-layer loop tests
(`tests/test_commands.py`, `tests/test_goal_lifecycle.py`) drive `cmd_run` with a
monkeypatched `build_executor` returning a `FakeExecutor`, so no test touches the
real SDK.
