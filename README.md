# crewd

> Multi-agent coding crew CLI — **Lead / Worker / Verifier / Advisory** running as separate Copilot CLI sessions, with GitHub Issues as the message bus.

`crewd` packages a multi-role autonomous coding crew into a reusable CLI. The roles are **Lead / Worker / Verifier**, with **Advisory as an optional fourth role** when you want proactive research and tradeoff analysis. Each role runs as an official **GitHub Copilot SDK session** (`backend: copilot-sdk`) with its own `config_directory` (pointed at `cfg/<role>/`, so its config + conversation are independent and resumable) and a per-role `agent.md` describing responsibilities. There is **no fixed round-robin**: the **Lead dynamically directs the crew** via a durable dispatcher. Each cycle the Lead is solicited and returns exactly one typed decision (`dispatch` a role, `continue_lead`, `wait`, `pause`, or `finish`); a dispatched role runs one attempt and returns exactly one typed handoff (`completed` / `no_progress`) that feeds the Lead's next decision. All dispatches, attempts, and handoffs are journaled to a SQLite run log for restart-safe, at-least-once, idempotent recovery.

The roles are decoupled from the target repo: the workspace lives wherever you want, the target repo is cloned into `<workspace>/repo/`, per-role git worktrees are created at `cfg/<role>/worktree/` (each role's cwd), and the only inter-role communication channel is GitHub issue / PR comments (plus an out-of-band human inbox).

---

## Quickstart

```bash
# 0. Install. crewd runs every role through the official GitHub Copilot SDK
#    (`backend: copilot-sdk`, the default and only production backend), which is
#    a required core dependency — so a plain install is fully runnable:
#      • from this repo (editable/dev):
cd ~/crewd && uv sync
#      • as a package (pick one):
#        pip install crewd        # or:  uv tool install crewd

# 1. Create a crew workspace (separate from your target repo)
mkdir -p ~/crews && cd ~/crews
uv --directory ~/crewd run crewd init my-crew --repo myorg/my-app

# 2. Edit the goal
cd my-crew
uv --directory ~/crewd run crewd goal --edit -w "$(pwd)"

# 3. Sanity check (config, families, target repo clone, agents/, inbox)
uv --directory ~/crewd run crewd doctor -w "$(pwd)"

# 4. Smoke test: one dispatcher step in the foreground (Lead is solicited,
#    then at most one role it dispatches runs one attempt)
uv --directory ~/crewd run crewd run --once -w "$(pwd)"

# 5. (debug/compat escape hatch) Force a single tick of ONE named role,
#    bypassing the Lead dispatcher — handy for isolating one role
uv --directory ~/crewd run crewd tick lead -w "$(pwd)"

# 6. Run until completed, human-blocked, max-cycles, or signal
uv --directory ~/crewd run crewd run --daemon -w "$(pwd)"

# 7. Check on it
uv --directory ~/crewd run crewd status -w "$(pwd)"

# 8. Stop gracefully (writes STOPPED + sends SIGINT to daemon)
uv --directory ~/crewd run crewd stop -w "$(pwd)"
```

> ⚠️ When invoking via `uv --directory ~/crewd run crewd`, **always pass `-w "$(pwd)"`** — `uv --directory` changes uv's resolution cwd to the crewd repo, so workspace auto-discovery would otherwise pick the wrong directory.

---

## Workspace layout

`cfg/advisory/` exists only when the Advisory role is configured.

```
my-crew/
├── crew.yaml                 # config (roles, models, families, loop, target)
├── GOAL.md                   # human-authored spec for this epoch
├── cfg/                      # per-role working directory + copilot config
│   ├── lead/
│   │   ├── AGENTS.md          # role instructions (Copilot auto-loads from cwd)
│   │   ├── session-state/
│   │   └── worktree/         # git worktree (isolated repo copy)
│   ├── worker/
│   │   ├── AGENTS.md
│   │   ├── session-state/
│   │   └── worktree/
│   ├── verifier/
│   │   ├── AGENTS.md
│   │   ├── session-state/
│   │   └── worktree/
│   └── advisory/
│       ├── AGENTS.md
│       ├── session-state/
│       └── worktree/
├── state/
│   ├── STOPPED               # sentinel: loop exits at next check
│   ├── PAUSED                # human/operator action required; goal remains open
│   ├── run.pid               # daemon PID (present only when daemon is running)
│   ├── cycle.txt             # legacy cycle mirror
│   ├── goal.json             # current epoch (version, label, sha, cycles)
│   ├── exit-reason           # written on graceful exit
│   ├── inbox/<role>.md       # operator → role messages
│   └── logs/<role>/<NNNN>.log
└── repo/                     # target repo clone (main branch)
```

Each role gets its own git worktree at `cfg/<role>/worktree/` created from the main clone. An `AGENTS.md` file is rendered into each worktree — Copilot CLI auto-loads it from cwd, replacing the previous prompt-injection approach.

---

## The roles

The default crew has four roles, but **Advisory is optional**. In practice:
- **Lead / Worker / Verifier** are the required core loop.
- **Advisory** is recommended for architecture, research, domain-heavy work, and tradeoff-sensitive goals.
- To disable Advisory, omit the `advisory:` entry from `crew.yaml`.

| Role         | Writes code? | Approves PRs? | Primary outputs                                 |
| ------------ | ------------ | ------------- | ----------------------------------------------- |
| **lead**     | no           | no            | plans, schedules, umbrella issues, prioritisation |
| **worker**   | yes          | no            | branches, commits, PRs                          |
| **verifier** | no           | **yes**       | per-PR review + final `crewd:acceptance` gate   |
| **advisory** | no           | no            | proactive research, tradeoffs, citations, risk spotting |

Hard rules baked into `doctor` and `run`:

- `worker.family ≠ verifier.family` (else verifier becomes a rubber stamp).
- All cross-role talk happens via GitHub issue / PR comments — never via shared files inside the workspace.
- Only the verifier merges. Only the worker pushes code.
- Two-tier verification: lightweight per-PR review + a heavy **Final Acceptance Gate** before the lead writes `STOPPED`.
- Advisory is **proactive but non-binding**: it should surface alternatives, tradeoffs, prior art, weak test oracles, and hidden risks, but it does not become the decision-maker.
- Ask the real user for input only in **true blocking cases**: unresolved product ambiguity, a material risk tradeoff, or strong cross-role disagreement that changes the shipped outcome. This should be rare.
- When human input, operator-only action, credentials, approval, or a future external event is the only remaining path, Lead records the exact escalation and writes `PAUSED`. The loop exits before ticking more roles and remains resumable without pretending the goal is complete.

### Role decision norms

- **Lead** keeps a lightweight decision log, actively invites Advisory on meaningful tradeoffs, and makes disagreement explicit instead of letting it stay implicit.
- **Worker** must not silently redefine success when implementation gets hard; each PR should include a short implementation note covering approach, tradeoffs, and remaining uncertainty.
- **Verifier** should perform a quick spec attack (`what is the weakest interpretation this PR could satisfy?`) and stay alert for regression risk even when tests are green.
- **Advisory** should prefer broad-view strategic insight over micromanagement and use a structured lens: observation → options → tradeoffs → recommendation → confidence.

---

## Command reference

| Command                                  | Purpose                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------ |
| `init <path> [--name N --repo R]`        | Scaffold a new workspace + register it.                                        |
| `attach <owner/repo> [--branch --no-clone]` | Attach (or re-attach) target repo, clone into `repo/`, create per-role worktrees. |
| `doctor`                                 | Status dashboard with diagnostics (roles, state, inbox, recent logs, issues). |
| `refresh`                                | Re-render agents/ + AGENTS.md; migrate old workspace layout if needed.         |
| `goal [--edit] [--from FILE]`            | Print, `$EDITOR`-edit, or install `GOAL.md` from a file.                       |
| `run [--once] [--role R] [--daemon] [--no-auto-render]` | Foreground loop (default) or background daemon (`--daemon`). `--once` runs a single dispatcher step (Lead solicited + at most one dispatched attempt), not a full round of every role. `--role R` bypasses the dispatcher to force one named role. |
| `tick <role>`                            | Debug/compat escape hatch: one tick of a single named role, bypassing the Lead dispatcher (alias for `run --role`). |
| `stop [--reason] [--force]`              | Write `STOPPED` + signal daemon (`SIGINT`; `--force` sends `SIGKILL`).         |
| `pause "<reason>"`                       | Write `PAUSED`; loop exits with `human-blocked` and keeps goal issues open.    |
| `resume`                                 | Clear `STOPPED` and `PAUSED`.                                                  |
| `status`                                 | Compact one-table status (includes daemon PID + alive check).                  |
| `logs [--role R] [--cycle N] [-n N] [-f]` | List or tail role logs.                                                       |
| `list [--prune]`                         | List registered workspaces (user-level registry).                              |
| `cd <name>`                              | Print abs path of a registered workspace (use as `cd $(crewd cd foo)`).        |
| `talk <role> "<msg>"`                    | Append a free-form operator message to `state/inbox/<role>.md`.                |
| `inbox <role> <OVERRIDE\|ADVICE\|INFO> "<msg>"` | Append a prioritised operator line to the inbox.                         |
| `new-goal --from GOAL.md`                | Bump goal epoch: copy GOAL.md, close prior `goal:vN` issues, reset cycles, and queue inbox override notices for all roles. |

`-w / --workspace <path>` is accepted by all workspace-scoped commands. Without it, `crewd` walks up from cwd looking for `crew.yaml` (git-style discovery).

### Human-blocked pause

`STOPPED` means completed or manually stopped. `PAUSED` means the goal is still open but
cannot advance without a human/operator action. Lead must first exhaust autonomous work,
post the exact blocker and requested action on the task and umbrella issues, then write a
single-line `state/PAUSED` reason beginning with `human-blocked:`. The loop exits after
Lead's decision, so Advisory, Worker, and Verifier are not invoked pointlessly.

After resolving the blocker:

```bash
crewd resume -w /path/to/workspace
crewd run -w /path/to/workspace --daemon
```

---

## Configuration (`crew.yaml`)

```yaml
name: my-crew
target:
  remote: myorg/my-app        # GitHub owner/name; null until attached
  branch: main
  repo: ./repo                # local clone path (relative to workspace)
goal_file: ./GOAL.md
roles:
  lead:     {model: claude-sonnet-4.6, family: claude}
  worker:   {model: gpt-5.4,           family: gpt}
  verifier: {model: claude-sonnet-4.6, family: claude}
  advisory: {model: gpt-5.4,           family: gpt}   # optional
  # omit advisory entirely for a 3-role crew
  # per-role override:
  # worker: {model: ..., family: ..., per_tick_timeout: 1800}
loop:
  sleep_secs: 60
  per_tick_timeout: 900       # default per-role timeout
  max_cycles: 0               # 0 = forever
backend: copilot-sdk          # official GitHub Copilot SDK sessions (default; legacy `copilot` is retired)
extra_add_dirs:               # optional: extra host dirs every role can access
  - /home/me/web-deploy       #   (deploy checkouts, persistent data dirs, …)
  - ../shared-data            #   relative entries resolve against the workspace
  # Missing paths are silently skipped; only existing dirs are passed to the agent.
```

Edit `crew.yaml` at any time — on the next `run` / `tick`, `agents/*.agent.md` is auto re-rendered from the templates if `crew.yaml` is newer (disable with `--no-auto-render`).

---

## Operator workflow

**Out-of-band nudge a role**

```bash
crewd talk worker "small PRs only — split #42 into 3 PRs"
crewd inbox lead OVERRIDE "drop feature X, focus on auth bug"
```

The host (orchestrator) consumes the role's inbox file when it constructs that role's next dispatched prompt — it injects the messages inline under an `OPERATOR INBOX` banner (highest priority first) and moves the file to `state/inbox/<role>.processed.<ts>.md` (preserving an audit trail) only after that attempt finishes. The role does not read or clear the file itself, so an operator `OVERRIDE` cannot be silently skipped. `OVERRIDE` outranks the role's own plan; `ADVICE` is treated as a strong suggestion; `INFO` is context only.

**Start a new goal on the same workspace**

```bash
$EDITOR GOAL.md          # rewrite the goal
crewd new-goal --from GOAL.md
crewd run                 # lead picks up [OVERRIDE] inbox notice with new label
```

`new-goal` bumps `goal:vN`, closes prior open issues with the previous label, resets cycles, clears `STOPPED`, re-renders `agents/`, and queues `[OVERRIDE]` inbox notices for all configured roles. Required when reusing a workspace for a fresh goal — otherwise resumed sessions may follow the old goal state or old acceptance result instead of re-grounding on the new epoch.

**Graceful shutdown**

`run` installs `SIGINT` / `SIGTERM` handlers that finish the current attempt and then write `state/exit-reason`. Send a second signal to abort hard. Mid-attempt, the orchestrator requests a single non-blocking `CancelToken` abort of the in-flight SDK session; if the abort cannot be confirmed idle, that session is tainted and force-stopped so the next run starts a fresh generation rather than resuming a dirty session.

---

## Failure recovery

| Symptom                                                       | Fix                                                                                |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `STOPPED` present at cycle 0 (doctor flags it)                | `crewd resume && crewd run`                                                        |
| Copilot session resume fails (`CAPIError 400` / broken session-state)         | Rare — a tainted session auto-advances to a fresh generation on the next run. To force it, `mv cfg/<role>/session-state cfg/<role>/session-state.broken-$(date +%s)` then re-run. |
| `family check: worker.family == verifier.family`              | Edit `crew.yaml` so they differ; rerun.                                            |
| `target repo clone missing`                                   | `crewd attach <owner/repo> --clone`                                                |
| `GOAL.md changed since goal vN started`                       | `crewd new-goal --from GOAL.md` to start a new epoch.                              |
| Lead immediately writes `STOPPED` after restart with new GOAL | You forgot `new-goal`. Run it; verify `state/inbox/lead.md` has `[OVERRIDE]`.      |

---

## Status

- Backend: **official GitHub Copilot SDK** (`backend: copilot-sdk`, default). The legacy `gh copilot` subprocess backend is retired — selecting `backend: copilot` fails with a migration error.
- Tested on Linux (Azure VM). Templates live in `src/crewd/templates/agents/*.j2`.
- See `tests/` for unit coverage; integration testing is via dogfooding on real repos.

---

## Repo layout

```
src/crewd/
  cli.py                — typer entrypoint
  commands.py           — command implementations
  config.py             — pydantic schema for crew.yaml + GoalState
  workspace.py          — path layout + sentinels
  orchestrator.py       — Lead-directed run loop (replaces round-robin); pre-send journal identity + cancellable attempts
  dispatcher.py         — durable SQLite run journal (dispatch/attempt/handoff/solicitation), restart-safe reconcile
  executor.py           — SdkAttemptExecutor: runs one role/lead attempt → typed handoff/decision + durable log
  sdk_adapter.py        — official Copilot SDK runtime + typed tools (submit_lead_decision / submit_role_handoff)
  session_backend.py    — session registry, goal-scoped ids + generations, CancelToken, taint store, outcomes
  diagnostics.py        — read-only operator status surface (snapshot + safe next action, bounded redaction)
  backends.py           — thin SdkBackend exit-code adapter (retires legacy `copilot` with a migration error)
  registry.py           — user-level workspace registry (~/.crewd/registry.json)
  templates_render.py   — jinja2 helpers
  templates/
    GOAL.md.j2
    agents/
      lead.agent.md.j2
      worker.agent.md.j2
      verifier.agent.md.j2
      advisory.agent.md.j2
SKILL.md                — instructions for AI agents operating crewd
docs/
  retrospective-orchestration.md — evidence baseline for the SDK-native, Lead-directed refactor
  sdk-backend.md                 — SDK-native role backend: transport decision, lifecycle SM, capability facts
  dispatcher.md                  — durable dispatch journal: schema, invariants, restart reconciliation
  orchestrator.md                — Lead-directed run loop, cancellation, operator diagnostics
scripts/
  live_smoke.py                  — bounded integrated live SDK smoke (see docs/sdk-backend.md)
```

---

## Design docs

- [`docs/retrospective-orchestration.md`](docs/retrospective-orchestration.md) — evidence-led
  retrospective of prior crew histories. Establishes the orchestration, prompt, observability,
  and recovery requirements that the SDK-native, Lead-directed refactor is built against.
- [`docs/sdk-backend.md`](docs/sdk-backend.md) — SDK-native role backend (`backend: copilot-sdk`):
  transport decision (per-role stdio), the one-attempt lifecycle state machine, offline-verified
  `github-copilot-sdk` capability facts, the (now resolved) capability risks, and the bounded
  live-smoke procedure.
- [`docs/dispatcher.md`](docs/dispatcher.md) — durable dispatch kernel: journal schema, exclusive
  Lead authority, at-least-once idempotent handoffs, restart reconciliation, and schema migration.
- [`docs/orchestrator.md`](docs/orchestrator.md) — Lead-directed run loop: exit reasons, pre-send
  journal identity, the exactly-one typed channel, mid-attempt cancellation, and operator diagnostics.

License: internal / unreleased.
