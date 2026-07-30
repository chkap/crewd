# crewd

> Multi-agent coding crew CLI — **Lead / Worker / Verifier / Advisory** running as separate Copilot CLI sessions, with GitHub Issues as the message bus.

`crewd` packages a multi-role autonomous coding crew into a reusable CLI. The roles are **Lead / Worker / Verifier**, with **Advisory as an optional fourth role** when you want proactive research and tradeoff analysis. Each role runs as an official **GitHub Copilot SDK session** (`backend: copilot-sdk`) with its own `config_directory` (pointed at `cfg/<role>/`, so its config + conversation are independent and resumable) and a per-role `agent.md` describing responsibilities. There is **no fixed round-robin**: the **Lead dynamically directs the crew** via a durable dispatcher. Each cycle the Lead is solicited and returns exactly one typed decision (`dispatch` a role, `continue_lead`, `wait`, `pause`, or `finish`); a dispatched role runs one attempt and returns exactly one typed handoff (`completed` / `no_progress`) that feeds the Lead's next decision. All dispatches, attempts, and handoffs are journaled to a SQLite run log for restart-safe, at-least-once, idempotent recovery.

The roles are decoupled from the target repo: the workspace lives wherever you want, the target repo is cloned into `<workspace>/repo/`, per-role git worktrees are created at `cfg/<role>/worktree/` (each role's cwd), and the only inter-role communication channel is GitHub issue / PR comments (plus an out-of-band human inbox).

- **Project home:** https://github.com/chkap/crewd
- **Issues / support:** https://github.com/chkap/crewd/issues
- **License:** MIT — free and open source software.

---

## Prerequisites

crewd orchestrates other tools rather than reimplementing them, so a runnable install needs:

- **Python 3.11+** — the only supported runtime (`requires-python = ">=3.11"`).
- **A GitHub Copilot subscription.** Every role runs as an official GitHub Copilot SDK session (`backend: copilot-sdk`). The `github-copilot-sdk` runtime is bundled as a dependency, but it authenticates against your Copilot entitlement — an account without Copilot access cannot run roles.
- **The GitHub CLI (`gh`), authenticated.** crewd shells out to `gh` to clone the target repo, list/close epoch issues, and drive the public GitHub bus (post/verify attributed comments). Install `gh`, then `gh auth login` (or export `GH_TOKEN`) for the account that owns the target repo. The token must have **repo read/write** scope on the target — see [Security & repository-write implications](#security--repository-write-implications).
- **git** on `PATH` — crewd creates the clone and per-role worktrees with local git.

Verify prerequisites before installing:

```bash
python --version         # >= 3.11
gh auth status           # logged in, with repo scope on the target org/repo
git --version
```

---

## Installation

crewd is a CLI application. Install it so the `crewd` command is on your `PATH` — the recommended tools give each install an isolated environment:

```bash
# pipx (recommended for a CLI): isolated venv, on PATH
pipx install crewd

# uv tool: same idea, using uv
uv tool install crewd

# plain pip (into the current/user environment)
pip install --user crewd
```

Confirm the entry point and version:

```bash
crewd --help
crewd --version    # or:  python -c "import crewd; print(crewd.__version__)"
```

> **Upgrading:** `pipx upgrade crewd`, `uv tool upgrade crewd`, or `pip install --upgrade crewd`. See [Upgrading an existing workspace](#upgrading-an-existing-workspace) for migrating a workspace created by an older crewd.

Once installed, every example below uses the bare `crewd` command with git-style workspace discovery — no repository checkout is required.

---

## Quickstart (minimal SDK-native flow)

```bash
# 1. Create a crew workspace (kept separate from your target repo)
mkdir -p ~/crews && cd ~/crews
crewd init my-crew --repo myorg/my-app     # clones myorg/my-app into my-crew/repo/
cd my-crew

# 2. Edit the goal for this epoch
crewd goal --edit

# 3. Sanity check: config, worker≠verifier family, target clone, agents/, inbox
crewd doctor

# 4. Smoke test: one dispatcher step (Lead is solicited, then at most one role it
#    dispatches runs a single attempt) — proves auth + bus end to end
crewd run --once

# 5. Run until goal-complete, human-blocked, max-cycles, or a signal
crewd run --daemon

# 6. Check on it, then stop gracefully (writes STOPPED + SIGINTs the daemon)
crewd status
crewd stop
```

All workspace-scoped commands accept `-w / --workspace <path>`. Without it, `crewd` walks up from the current directory looking for `crew.yaml` (git-style discovery), so running from inside `my-crew/` just works.

> **Debug/compat escape hatch:** `crewd tick <role>` forces a single tick of one named role, bypassing the Lead dispatcher — handy for isolating one role. Normal runs go through `crewd run`.

<details>
<summary>Running from a source checkout (contributors)</summary>

If you're hacking on crewd itself rather than using an installed wheel, run the in-tree CLI through uv from the repo you cloned:

```bash
git clone https://github.com/chkap/crewd && cd crewd && uv sync
# From inside a workspace directory, always pass -w so uv's --directory cwd
# (the crewd repo) doesn't defeat workspace auto-discovery:
uv --directory /path/to/crewd run crewd doctor -w "$(pwd)"
```

This form is for development only; installed users should use the bare `crewd` command above.
</details>

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
│   ├── public_writes/        # durable public-bus intents (reserve→verify→reconcile)
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

## The public bus (canonical GitHub coordination contract)

crewd's coordination is **public-first**: every material inter-role message — each Lead routing decision that dispatches a role, and each role handoff back to Lead — is a real, attributed GitHub issue/PR artifact. The host (not model best-effort) publishes and verifies these before authority advances, so the run's routing history is auditable on GitHub and survives restarts.

**Canonical attribution.** Every crew-authored artifact begins with one parseable first line:

```
> **[crewd:<role> -> <target>]** <crew-name>
```

e.g. `> **[crewd:worker -> verifier]** my-crew`. A body missing or malforming this line is rejected before posting.

**Idempotency / correlation.** Each write carries a stable, invisible correlation marker (`<!-- crewd:correlation:<id> -->`). `PublicWriter.post` **reserves** a durable intent, **searches** for the marker (so a crashed or ambiguous retry never double-posts), **writes** with the attribution line + marker, then **verifies** the returned URL before marking the intent verified. Intents live under `state/public_writes/` (one JSON file per correlation id) in one of two states: `reserved` (post attempted, URL not yet verified) or `verified`.

**Restart reconciliation.** On every `crewd run`, the orchestrator reconciles reserved-but-unverified intents *before* new work: it re-scans for the marker and finalizes the intent from the landed artifact, or re-posts idempotently. A landed write is never duplicated; a still-unreachable GitHub leaves the intent `reserved` and surfaces in `status` / `doctor` rather than aborting the run.

**Material handoff published before consumption.** A material role handoff (a `completed` handoff, or a `no_progress` that carries evidence/changed/remaining/disagreement/blocker) must be a verified public artifact before Lead may consume it. A genuinely empty/non-material `no_progress` stays private. If the public write cannot be verified, the handoff is **not** consumed and authority does not advance.

**Prerequisite gating.** Before Lead dispatches **Worker** or **Verifier**, and before a Lead `finish` terminalises the goal, the public-bus gate validates the required GitHub record (active linked `crewd:task`, assignment/scope, and for finish a closed `crewd:acceptance` issue + public goal summary). The active task and acceptance references are derived from the public record, never hard-coded. A missing / invalid / unverifiable prerequisite **rejects** the transition **without** reserving the attempt, consuming any pending handoff, or terminalising the run — authority never advances on an unverified record. An explicit GitHub failure (rate limit, outage, ambiguous write) routes to a bounded wait/reconcile, never to a silent private advance.

**Offline / recovery.** Set `CREWD_DISABLE_PUBLIC_BUS=1` to run the dispatcher without the public bus (offline recovery / local mechanics only). This is a deliberate escape hatch — normal runs against an attached remote keep it enabled so coordination stays on GitHub.

---

## Security & repository-write implications

crewd is an **autonomous agent that acts on GitHub with your credentials**. Understand the blast radius before pointing it at a repository:

- **It writes to the target repo as you.** Roles use your authenticated `gh` / git identity to push branches, open PRs, comment on issues/PRs, and (verifier) **merge** PRs. Anything crewd does is attributable to, and authorized by, your token.
- **Scope the token to the target.** Authenticate `gh` (or `GH_TOKEN`) with the least privilege that still allows repo read/write on the target. Prefer a dedicated bot/service account or a fine-grained token limited to the one repository over a broad personal token.
- **The workspace holds session state, not secrets by policy.** `cfg/<role>/session-state/` (Copilot SDK conversations) and `state/` (run journal, public-bus intents, inbox) live under the workspace. Durable logs are redacted (GitHub tokens, JWTs, `bearer`/`authorization`/`api_key`-style values are stripped) before they are written, but treat the workspace directory as sensitive and keep it out of shared/committed locations.
- **`extra_add_dirs` cannot escape the workspace.** The Copilot SDK mounts a single working directory (the workspace root) with no `--add-dir` equivalent, so any `extra_add_dirs` entry that resolves outside the workspace (including a symlink to an external target) is a **non-runnable blocker**: `doctor` errors and `run` refuses at pre-flight (rc=2) before any SDK work, so context outside the crew is never exposed. Copy/sanitize only what a role needs into the workspace.
- **Merges are gated, not unconditional.** Only the verifier merges, behind a two-tier review plus a Final Acceptance Gate; only the worker pushes code. But these are policy guardrails enforced by prompts and the dispatcher — run crewd against repositories where an autonomous PR/merge is acceptable, and rely on branch protection / required reviews for a hard backstop.

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
extra_add_dirs:               # optional: additional in-workspace dirs to expose to roles
  - ./shared-data            #   relative entries resolve against the workspace root
  # The Copilot SDK mounts a single working_directory (the workspace root) and has
  # NO `--add-dir` equivalent, so entries must resolve INSIDE the workspace.
  # • internal path      → mounted (readable by every role)
  # • missing path       → skipped at run time (non-fatal warning)
  # • external path, or a symlink whose target is external → NON-RUNNABLE blocker:
  #   `doctor` reports an error and `run` pre-flight refuses (rc=2) before any
  #   backend/dispatch/SDK work. Recovery: copy or sanitize only the needed
  #   context into the workspace and point extra_add_dirs at that in-workspace
  #   path, so secrets outside the crew are never exposed.
```

Edit `crew.yaml` at any time — on the next `run` / `tick`, `agents/*.agent.md` is auto re-rendered from the templates if `crew.yaml` is newer (disable with `--no-auto-render`).

---

## Operator workflow

**Out-of-band nudge a role**

```bash
crewd talk worker "small PRs only — split #42 into 3 PRs"
crewd inbox lead OVERRIDE "drop feature X, focus on auth bug"
```

The host (orchestrator) consumes the role's inbox file when it constructs that role's next dispatched prompt — it injects the messages inline under an `OPERATOR INBOX` banner (highest priority first) and moves the file to `state/inbox/<role>.processed.<ts>.md` (preserving an audit trail) only after that attempt finishes. Delivery is a two-phase, host-owned handshake: on dispatch the live `<role>.md` (plus any orphaned staging from a crashed prior attempt) is atomically staged to `<role>.delivering.<attempt>.md`, and it is acknowledged into `.processed.<ts>.md` **only after the attempt durably terminalises**. A crash before acknowledgement retains the message for **redelivery** to the next attempt, so an operator `OVERRIDE` is never lost or consumed by the wrong role. The role does not read or clear the file itself, so an operator `OVERRIDE` cannot be silently skipped. `OVERRIDE` outranks the role's own plan; `ADVICE` is treated as a strong suggestion; `INFO` is context only. `crewd doctor` reports per-role `pending` / `delivering` / `processed` counts without leaking message content.

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
| `backend: migration required` / `backend: copilot ... has been removed` | Legacy workspace on the retired subprocess backend. `crewd refresh` migrates `crew.yaml` to `backend: copilot-sdk` (preserving unknown config keys + workspace state), then `crewd doctor`. |
| `extra_add_dirs entry ... resolves outside the workspace`     | External context is a non-runnable blocker (SDK has no `--add-dir`). Copy/sanitize the needed files into the workspace and repoint `extra_add_dirs` at the in-workspace path; re-run `crewd doctor`. |
| `public write(s) reserved but unverified` (doctor/status)     | A crash left a public-bus intent mid-post. `crewd run` reconciles it against GitHub (idempotent, no duplicate comment) once GitHub is reachable; if it persists, check GitHub connectivity/auth. |
| `family check: worker.family == verifier.family`              | Edit `crew.yaml` so they differ; rerun.                                            |
| `target repo clone missing`                                   | `crewd attach <owner/repo> --clone`                                                |
| `GOAL.md changed since goal vN started`                       | `crewd new-goal --from GOAL.md` to start a new epoch.                              |
| Lead immediately writes `STOPPED` after restart with new GOAL | You forgot `new-goal`. Run it; verify `state/inbox/lead.md` has `[OVERRIDE]`.      |
| `PAUSED` with `human-blocked:` reason                         | Resolve the stated action, then `crewd resume && crewd run` (a plain `run` never clears a durable blocker). |

---

## Upgrading an existing workspace

Upgrading the crewd package never rewrites your workspaces in place — reconcile each workspace on its next use:

```bash
pipx upgrade crewd          # or: uv tool upgrade crewd / pip install --upgrade crewd
cd /path/to/workspace
crewd refresh               # re-render agents/ + AGENTS.md, migrate old layout/backend if needed
crewd doctor                # confirm 0 errors before resuming
```

`crewd refresh` re-renders `agents/*.agent.md` from the current templates and migrates a legacy workspace — including a `crew.yaml` still on the retired `backend: copilot` subprocess transport → `backend: copilot-sdk` — while **preserving** unknown config keys and durable state (`STOPPED`/`PAUSED`, `goal.json`, `session-state/`, `public_writes/`). It is idempotent, so re-running it is safe. Agent templates are also auto re-rendered on the next `run`/`tick` when `crew.yaml` is newer (disable with `--no-auto-render`).

---

## Limitations

crewd 0.1.0 is an early public release. Known constraints:

- **GitHub-only bus.** Coordination is GitHub Issues/PRs; there is no GitLab/Bitbucket/other-forge backend. A reachable GitHub remote and an authenticated `gh` are required for normal (non-offline) runs.
- **Copilot SDK is the only backend.** `backend: copilot-sdk` is the default and only production transport; the legacy `copilot -p` subprocess backend is retired and rejected at pre-flight. A GitHub Copilot subscription is required to run roles.
- **Single mounted directory.** The SDK mounts one working directory (the workspace root) with no `--add-dir` equivalent, so all role-visible context must live inside the workspace (see `extra_add_dirs`).
- **One run per workspace.** Do not run two `crewd run` processes against the same workspace; the durable journal assumes a single active runner.
- **The public API is the CLI.** The importable `crewd` package ships no `py.typed` marker and makes no typed-library stability guarantee; only the `crewd` command line is a supported contract.
- **Primarily exercised on Linux.** Development and dogfooding are on Linux; other platforms are unvalidated. Integration coverage is via dogfooding on real repos plus the unit suite in `tests/`.

---

## Status

- Backend: **official GitHub Copilot SDK** (`backend: copilot-sdk`, default). The legacy `gh copilot` subprocess backend is retired — selecting `backend: copilot` fails with a migration error.
- Tested on Linux (Azure VM). Templates live in `src/crewd/templates/agents/*.j2`.
- See `tests/` for unit coverage; integration testing is via dogfooding on real repos.
- Changelog: [`CHANGELOG.md`](CHANGELOG.md).

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

License: MIT (see [`LICENSE`](LICENSE)). crewd is free and open source software.
