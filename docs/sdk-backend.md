# SDK-native role backend (`backend: copilot-sdk`)

Status: **operational for a single-role tick (#10)**. Selecting
`backend: copilot-sdk` runs a role through a real Copilot **session** via the
adapter + attempt state machine and maps the typed outcome back to the legacy
exit code. Replacing fixed round-table scheduling and consuming the richer
persisted handoffs is #11 (which reads the `<log>.attempt.json` sidecar this
backend writes, rather than reconstructing lifecycle meaning from exit codes).

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
| `crewd.backends.SdkBackend` | `Backend`-shaped entry (`doctor`/`run_role`) selected by `backend: copilot-sdk`; maps outcome→exit code. | Lazy, via the adapter |

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
- **Unconfirmed cleanup is not resumable.** If `disconnect()` cannot be
  confirmed — even on an otherwise successful `IDLE_COMPLETED` attempt — the
  session id is tainted (`cleanup_confirmed=False`), so the next tick starts a
  fresh session instead of silently resuming a half-torn-down one (R4/R5).
- **Tainted sessions refuse resume** unless `allow_tainted_resume=True` is
  passed explicitly (operator override). The backend's automatic recovery for a
  tainted active session is to **advance the recovery generation to a brand-new
  session id and create fresh** — it never reuses the force-stopped id and never
  clears the old taint record (see *Session identity* below).
- **Durable logs are redacted.** `redact()` strips GitHub tokens, JWTs, and
  `bearer`/`authorization`/`api_key`-style secrets before anything (lifecycle
  events *or* drained SDK event summaries) is written to the log.

### Session identity — `(workspace, goal epoch, role, recovery generation)`

`build_session_id(workspace_id, goal_label, role, generation=0)` returns a
deterministic hash. Identity is scoped by **all four** components, not merely the
role directory (which is stable across goal epochs). `SessionRegistry` persists
`(goal_label, role) → {active session id, generation}` as atomically-written
JSON (`config_dir/.crewd-sdk-sessions.json`):

- **Normal tick** resumes the active id for the current `(epoch, role)`.
- **New goal epoch** (`crewd new-goal` bumps `goal:vN`) → a new registry key →
  generation 0 → a **fresh** session, never a resume of the prior epoch's
  conversation.
- **Taint** of the active id → generation advances to a **new** id (create), and
  the old tainted id is left in the taint store for audit. This matches the
  SDK's persistence semantics: `force_stop()` clears in-memory sessions but does
  **not** destroy persisted state, so reusing the same id would risk colliding
  with untrusted on-disk state (Advisory). A new-generation id sidesteps that.

## Backend wiring (`SdkBackend.run_role`)

`SdkBackend` maps the legacy `Backend.run_role(...)` call onto the state machine.
`_tick_role` threads `goal_label` and `workspace_root` to the boundary
explicitly (they are not inferred from stable role directories):

- **model / directories / prompt / timeout** → `SdkRoleRuntime` construction +
  `AttemptConfig(wait_timeout=timeout)`; `working_directory = workspace root`
  (see mounting policy below), `config_directory = config_dir`.
- **session id + resume** come from `SessionRegistry.decide(goal_label, role,
  taint_store)` (above) — **not** the CLI's `first_run` (`session-state/`
  presence). A legacy workspace switching to the SDK backend does not assume an
  SDK session exists.
- **outcome → exit code** at the compatibility edge:
  `idle_completed→0`, `aborted_clean→130`, `tainted→124`, `sdk_error→1`
  (mirroring `CopilotBackend`'s SIGINT/SIGKILL conventions). The **typed**
  result (incl. `goal_label`, `generation`) is persisted to `<log>.attempt.json`
  for #11.
- **cleanup** is owned by the state machine (disconnect / force_stop).
- `ops_factory` is injectable so the whole selectable path is covered by
  deterministic tests without the SDK (`tests/test_config.py`).

### Workspace mounting — fail closed, don't run blind

The SDK exposes a single `working_directory` and has **no `--add-dir`
equivalent**, so every path the role needs must live under one mountable root.
The role's required context — workspace `GOAL.md`, `state/inbox`, the role's
config dir (`AGENTS.md`), and the role's git worktree — **all live under the
workspace root**, so `run_role` mounts the **workspace root** as
`working_directory`. `config_directory` stays the per-role config dir for
session-state isolation.

Any requested `add_dir` (e.g. a configured `extra_add_dirs` entry) that is **not
under the workspace root** genuinely cannot be mounted. Rather than log a warning
and run the role without required context, `run_role` **refuses to start the
attempt** (`PRE-EXECUTION ERROR`, exit code 1) and names the offending paths.
Fix: move the path under the workspace, or keep `backend: copilot` for cross-dir
roles.

> Open verification (live-smoke): confirm that `working_directory = workspace
> root` + `config_directory = role config dir` still auto-loads only the
> intended role `AGENTS.md`/instructions and does not weaken role write
> isolation. Until verified live, we do **not** substitute symlinks or a
> narrower working dir.

### Tool-permission compatibility policy

The legacy `copilot -p` backend ran with `--allow-all-tools` so roles could do
their git/GitHub/file work. To keep `backend: copilot-sdk` *operationally*
equivalent (not merely importable), the adapter defaults to the SDK's **own**
`PermissionHandler.approve_all`, invoked by the SDK as
`handler(request, {"session_id": ...})` and returning the typed
`PermissionDecisionApproveOnce` (not a `{"result": ...}` dict — that would
silently fail the first permissioned op). A fail-closed handler
(`allow_all_tools=False`) returns the typed `PermissionDecisionUserNotAvailable`
— the same decision the SDK falls back to when no handler can satisfy a request.

## Offline-verified SDK capability facts (`github-copilot-sdk` 1.0.8)

Verified by importing the package and inspecting signatures (module name is
`copilot`). The runtime was **not** launched (see live-smoke below).

- Async API throughout. `session.session_id` is the id.
- `CopilotClient(working_directory=...)`, `.start()`, `.stop()`, `.force_stop()`.
- `.create_session(session_id=, model=, working_directory=, config_directory=,
  available_tools=, excluded_tools=, on_permission_request=, hooks=,
  reasoning_effort=, on_event=)` and `.resume_session(session_id, ...)`.
- `session.send_and_wait(prompt, timeout=)` waits for `session.idle` and returns
  the last assistant-message event, **which may legitimately be `None`** when
  idle is reached with no assistant message. A wait *timeout* is signalled by
  **`TimeoutError`**, *not* by a `None` return. The adapter's idle oracle is
  therefore "the call returned without raising", and a `None` return is
  `IDLE`, not a timeout (corrected per Advisory, from official source).
- `session.abort()`, `session.get_events() -> list[SessionEvent]`,
  `session.disconnect()`.
- Default transport = SDK-owned child stdio (`StdioRuntimeConnection`).

## Open capability risks (need the live experiment)

1. **No `--add-dir` equivalent** (see mounting policy above). Required workspace
   paths are covered by mounting the workspace root; genuinely out-of-workspace
   `extra_add_dirs` **fail closed** pre-execution. The live experiment must
   confirm whether a hook could ever mount an extra out-of-workspace dir, and
   verify the workspace-root working-dir does not weaken role config/write
   isolation.
2. **Abort-confirmation semantics.** The adapter confirms cancellation by
   polling the durable event history for the SDK's `abort`/idle marker **without
   sending a new turn** (an empty `send_and_wait` would start a fresh turn — the
   bug Advisory flagged). A real-time *pre-registered idle latch* (Advisory
   option 1, matching the official E2E test) is stronger and is the documented
   next step once the live smoke pins the exact event type.
3. **Event ordering / idle detection.** Which concrete `SessionEvent` type marks
   "turn idle" / "aborted" vs. "still working" must be pinned against a real
   runtime; `_is_abort_or_idle_event` currently matches tolerantly by type name.

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
       print("idle (no exception) events:", len(await s.get_events()))
       await s.disconnect(); await c.stop()
   asyncio.run(main())
   ```
3. Resolve each open risk above and record findings back into this file:
   - the concrete idle/working/abort `SessionEvent` types (then tighten
     `_is_abort_or_idle_event` and consider the pre-registered idle latch),
   - whether `abort()` needs an idle poll or exposes a waitable,
   - the `extra_add_dirs` answer.

## Config

`config.py` accepts `backend: "copilot" | "copilot-sdk"` (default `"copilot"`).
Selecting `copilot-sdk` runs `SdkBackend.doctor()` in preflight (verifies
`import copilot` succeeds) and then executes each role tick through a real SDK
session. It never silently falls back to `copilot -p`.

