# SDK-native role backend (`backend: copilot-sdk`)

Status: **capability + seam established (#10)**. Tick-loop wiring is deferred to
#11. This document records the transport decision, the offline-verified SDK
capability facts, the attempt lifecycle state machine, the open capability
risks, and a bounded live-smoke procedure.

It is the design baseline for replacing the `copilot -p` subprocess backend
(`crewd.backends.CopilotBackend`) with the official
[`github-copilot-sdk`](https://pypi.org/project/github-copilot-sdk/) programmatic
session API, per requirements **R4–R6** in
[`retrospective-orchestration.md`](retrospective-orchestration.md).

## Why (evidence)

The retrospective quantified the core defect of the CLI backend: it measures
**cumulative** footer timing and cannot distinguish *"the role finished its
turn"* from *"the wait window elapsed while work is still in flight"*. That
conflation is what produced the legal-crew cycle-77 timeout→SIGKILL and the
`fin` aborted-epoch gaps. The SDK gives us:

- a first-class **session** object with typed events (idle vs. still-working),
- an explicit `abort()` distinct from a wait timeout, and
- deterministic, resumable `session_id`s.

The new boundary is built so those distinctions are **structural** (enforced by
the state machine + tests), not conventions.

## Module layout

| Module | Role | SDK import? |
|--------|------|-------------|
| `crewd.session_backend` | Domain port + attempt state machine + taint store. Fully unit-testable. | **No** — SDK-independent |
| `crewd.sdk_adapter` | Concrete `SdkOps` over `copilot.CopilotClient`; async→sync bridge. | Yes (lazy, inside `open()`) |
| `crewd.backends.SdkBackend` | `Backend`-shaped entry (`doctor`/`run_role`) selected by `backend: copilot-sdk`. | Lazy, for `doctor()` only |

`session_backend` defines the narrow `SdkOps` Protocol
(`open`/`run`/`abort`/`drain_events`/`disconnect`/`force_stop`). The real adapter
and the deterministic `FakeOps` in the tests both implement it, so the state
machine is exercised without auth or network.

## Transport decision — option 1: per-role SDK-owned stdio child

The SDK defaults to a `StdioRuntimeConnection` where the SDK **owns** a child
runtime process it launches and talks to over stdio. Considered options:

1. **Per-role `CopilotClient` over SDK-owned stdio** *(chosen)* — one client
   per role, each with its own child runtime, `working_directory` and
   `config_directory` set per role.
2. Shared client, multiple sessions — one runtime multiplexing all roles.
3. External runtime over TCP — operator manages a long-lived runtime; SDK
   connects to it.

**Chosen: option 1.** Rationale:

- **Isolation matches the crew model.** Each role already has its own worktree,
  config dir, and log. A per-role runtime keeps a crash/taint contained to one
  role instead of taking down the whole crew (option 2's failure blast radius).
- **Simplest correct lifecycle.** `start → create/resume → run → disconnect →
  stop`, with `force_stop` as the taint escape hatch, maps 1:1 to one child. No
  cross-role scheduling inside a shared runtime.
- **No new operational surface.** Option 3 needs an externally managed runtime
  and port/auth wiring — more moving parts for no benefit at current crew sizes.

Tradeoff: N child runtimes cost more RAM than one shared runtime. Crews are
small (≤ a handful of roles), so this is acceptable; option 2 remains a future
optimization behind the same `SdkOps` seam if profiling ever demands it.

## Attempt lifecycle state machine

One `run_attempt(...)` call = **one** role turn and yields **exactly one**
terminal `AttemptOutcome`. Per-attempt timing is monotonic and **not**
cumulative (the CLI backend's core defect).

```
                 ┌─────────────┐
   tainted &     │  (start)    │
   !override ───▶│ refuse open │──▶ SDK_ERROR (TaintedSessionError)
                 └──────┬──────┘
                        │ open(resume)
                        ▼
                    ┌───────┐  open raises ──▶ SDK_ERROR
                    │ opened│
                    └───┬───┘
                        │ run(prompt, wait_timeout)
              ┌─────────┴──────────┐
        IDLE  │                    │  WAIT_TIMEOUT       run raises ──▶ SDK_ERROR
              ▼                    ▼
      IDLE_COMPLETED        abort(abort_timeout)
       (resumable)         ┌────────┴─────────┐
                     true  │                  │ false / raises
                           ▼                  ▼
                    ABORTED_CLEAN        force_stop()  ──▶  TAINTED
                     (resumable)         (always taints; persisted to
                                          TaintStore; resume refused next
                                          time unless allow_tainted_resume)
```

Invariants (all covered by `tests/test_session_backend.py`):

- **Exactly one terminal outcome** per attempt (`AttemptResult.__post_init__`
  asserts the outcome is terminal).
- **Wait timeout ≠ cancellation.** `RunSignal.WAIT_TIMEOUT` never by itself
  marks success or taints; it only *triggers* a bounded `abort()`.
- **`force_stop` always taints.** Even if `force_stop()` itself raises, the
  session is still recorded `TAINTED` (fail-safe, not fail-open).
- **Tainted sessions refuse resume** unless `allow_tainted_resume=True` is
  passed explicitly (operator override).
- **Durable logs are redacted.** `redact()` strips GitHub tokens, JWTs, and
  `bearer`/`authorization`/`api_key`-style secrets before anything is written to
  the adapter-owned event log.

`build_session_id(workspace_id, goal_label, role)` returns a deterministic hash
so the same role in the same workspace/epoch resumes the same session.

## Offline-verified SDK capability facts (`github-copilot-sdk` 1.0.8)

Verified by importing the package and inspecting signatures (module name is
`copilot`). The runtime was **not** launched (see live-smoke below).

- Async API throughout. `session.session_id` is the id.
- `CopilotClient(working_directory=...)`, `.start()`, `.stop()`, `.force_stop()`.
- `.create_session(session_id=, model=, working_directory=, config_directory=,
  available_tools=, excluded_tools=, on_permission_request=, hooks=,
  reasoning_effort=, on_event=)` and `.resume_session(session_id, ...)`.
- `session.send_and_wait(prompt, timeout=) -> SessionEvent | None`. **A timeout
  returns `None` but does NOT abort in-flight work** — this is exactly why the
  state machine treats wait-timeout and abort as distinct steps.
- `session.abort()`, `session.get_events() -> list[SessionEvent]`,
  `session.disconnect()`.
- Default transport = SDK-owned child stdio (`StdioRuntimeConnection`).

## Open capability risks (need the live experiment)

1. **No `--add-dir` equivalent.** The CLI backend passes `add_dirs` so a role can
   read sibling crew dirs. `create_session` exposes `working_directory` /
   `config_directory` but **no multi-add-dir**. `extra_add_dirs` mapping is
   unresolved and flagged; the live experiment must confirm whether a hook or a
   symlinked working dir covers it.
2. **Abort-confirmation semantics.** We confirm a clean abort by checking the
   session returns to idle within the abort bound. Whether `abort()` is
   synchronous or needs an idle-event poll must be validated live; today the
   adapter treats an unconfirmed abort as taint (safe default).
3. **Event ordering / idle detection.** Which concrete `SessionEvent` type marks
   "turn idle" vs. "still working" must be pinned against a real runtime.

## Live-smoke procedure (bounded, run manually with auth)

The SDK downloads and launches a runtime binary that needs network, a writable
home, and Copilot auth. That is **sandbox-blocked** in CI/agent environments, so
this is a manual, bounded checklist — not an automated test:

1. Install the extra: `uv pip install -e '.[sdk]'` in an environment with
   Copilot auth configured.
2. Smoke a trivial turn:
   ```python
   import asyncio
   from copilot import CopilotClient
   async def main():
       c = CopilotClient(working_directory=".")
       await c.start()
       s = await c.create_session(model="claude-sonnet-4.6")
       ev = await s.send_and_wait("Reply with the single word: ok", timeout=60)
       print("idle?", ev is not None, "events:", len(await s.get_events()))
       await s.disconnect(); await c.stop()
   asyncio.run(main())
   ```
3. Resolve each open risk above and record findings back into this file:
   - the concrete idle/working `SessionEvent` types,
   - whether `abort()` needs an idle poll,
   - the `extra_add_dirs` answer.
4. Only after those are pinned does #11 wire `SdkBackend.run_role` onto the
   Lead-directed dispatch loop.

## Config

`config.py` accepts `backend: "copilot" | "copilot-sdk"` (default `"copilot"`).
Selecting `copilot-sdk` runs `SdkBackend.doctor()` in preflight (verifies
`import copilot` succeeds). `run_role` intentionally raises `NotImplementedError`
pointing at #11 until the loop coupling lands — selecting the backend today
validates the environment without silently falling back to `copilot -p`.
