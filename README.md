# crewd

> Multi-agent coding crew CLI — **Lead / Worker / Verifier / Advisory** running as separate Copilot CLI sessions, with GitHub Issues as the message bus.

`crewd` packages a 4-role autonomous coding crew into a reusable CLI. One workspace per crew, attachable to any GitHub repo. Each role is a `gh copilot` session with its own `--config-dir` (so its conversation is independent and resumable), a per-role `agent.md` describing responsibilities, and a fixed-order round-table loop that ticks each role once per cycle.

The four roles are decoupled from the target repo: the workspace lives wherever you want, the target repo is cloned into `<workspace>/checkout/`, and the only inter-role communication channel is GitHub issue / PR comments (plus an out-of-band human inbox).

---

## Quickstart

```bash
# 0. Install (editable)
cd ~/crewd && uv sync

# 1. Create a crew workspace (separate from your target repo)
mkdir -p ~/crews && cd ~/crews
uv --directory ~/crewd run crewd init my-crew --repo myorg/my-app

# 2. Edit the goal
cd my-crew
uv --directory ~/crewd run crewd -w "$(pwd)" goal --edit

# 3. Sanity check (config, families, target checkout, agents/, inbox)
uv --directory ~/crewd run crewd -w "$(pwd)" doctor

# 4. Smoke test: one tick of one role
uv --directory ~/crewd run crewd -w "$(pwd)" tick lead

# 5. Run one full cycle in foreground
uv --directory ~/crewd run crewd -w "$(pwd)" run --once

# 6. Run the loop in the background until STOPPED, max-cycles, or signal
uv --directory ~/crewd run crewd -w "$(pwd)" run --daemon

# 7. Check on it
uv --directory ~/crewd run crewd -w "$(pwd)" status

# 8. Stop gracefully (writes STOPPED + sends SIGINT to daemon)
uv --directory ~/crewd run crewd -w "$(pwd)" stop
```

> ⚠️ When invoking via `uv --directory ~/crewd run crewd`, **always pass `-w "$(pwd)"`** — `uv --directory` changes uv's resolution cwd to the crewd repo, so workspace auto-discovery would otherwise pick the wrong directory.

---

## Workspace layout

```
my-crew/
├── crew.yaml                 # config (roles, models, families, loop, target)
├── GOAL.md                   # human-authored spec for this epoch
├── agents/                   # rendered from src/crewd/templates/agents/*.j2
│   ├── lead.agent.md
│   ├── worker.agent.md
│   ├── verifier.agent.md
│   └── advisory.agent.md
├── cfg/                      # per-role copilot --config-dir (private session-state)
│   ├── lead/session-state/
│   ├── worker/session-state/
│   ├── verifier/session-state/
│   └── advisory/session-state/
├── state/
│   ├── STOPPED               # sentinel: loop exits at next check
│   ├── run.pid               # daemon PID (present only when daemon is running)
│   ├── cycle.txt             # legacy cycle mirror
│   ├── goal.json             # current epoch (version, label, sha, cycles)
│   ├── exit-reason           # written on graceful exit
│   ├── inbox/<role>.md       # operator → role messages
│   └── logs/<role>/<NNNN>.log
└── checkout/                 # target repo clone (cwd for every role)
```

---

## The four roles

| Role         | Writes code? | Approves PRs? | Primary outputs                                 |
| ------------ | ------------ | ------------- | ----------------------------------------------- |
| **lead**     | no           | no            | plans, schedules, umbrella issues, prioritisation |
| **worker**   | yes          | no            | branches, commits, PRs                          |
| **verifier** | no           | **yes**       | per-PR review + final `crewd:acceptance` gate   |
| **advisory** | no           | no            | sourced research, citations, design pointers    |

Hard rules baked into `doctor` and `run`:

- `worker.family ≠ verifier.family` (else verifier becomes a rubber stamp).
- All cross-role talk happens via GitHub issue / PR comments — never via shared files inside the workspace.
- Only the verifier merges. Only the worker pushes code.
- Two-tier verification: lightweight per-PR review + a heavy **Final Acceptance Gate** before the lead writes `STOPPED`.

---

## Command reference

| Command                                  | Purpose                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------ |
| `init <path> [--name N --repo R]`        | Scaffold a new workspace + register it.                                        |
| `attach <owner/repo> [--branch --no-clone]` | Attach (or re-attach) target repo, clone into `checkout/`.                  |
| `doctor`                                 | Status dashboard with diagnostics (roles, state, inbox, recent logs, issues). |
| `goal [--edit] [--from FILE]`            | Print, `$EDITOR`-edit, or install `GOAL.md` from a file.                       |
| `run [--once] [--role R] [--daemon] [--no-auto-render]` | Foreground loop (default) or background daemon (`--daemon`). `--once` / `--role` as before. |
| `tick <role>`                            | Imperative single tick of one role (alias for `run --role`).                   |
| `stop [--reason] [--force]`              | Write `STOPPED` + signal daemon (`SIGINT`; `--force` sends `SIGKILL`).         |
| `resume`                                 | Clear `STOPPED`.                                                               |
| `status`                                 | Compact one-table status (includes daemon PID + alive check).                  |
| `logs [--role R] [--cycle N] [-n N] [-f]` | List or tail role logs.                                                       |
| `list [--prune]`                         | List registered workspaces (user-level registry).                              |
| `cd <name>`                              | Print abs path of a registered workspace (use as `cd $(crewd cd foo)`).        |
| `talk <role> "<msg>"`                    | Append a free-form operator message to `state/inbox/<role>.md`.                |
| `inbox <role> <OVERRIDE\|ADVICE\|INFO> "<msg>"` | Append a prioritised operator line to the inbox.                         |
| `new-goal --from GOAL.md`                | Bump goal epoch: copy GOAL.md, close prior `goal:vN` issues, reset cycles, requeue lead with `[OVERRIDE]`. |

`-w / --workspace <path>` is accepted by all workspace-scoped commands. Without it, `crewd` walks up from cwd looking for `crew.yaml` (git-style discovery).

---

## Configuration (`crew.yaml`)

```yaml
name: my-crew
target:
  repo: myorg/my-app          # null until attached
  branch: main
  checkout: ./checkout
goal_file: ./GOAL.md
roles:
  lead:     {model: claude-sonnet-4.6, family: claude}
  worker:   {model: gpt-5.4,           family: gpt}
  verifier: {model: claude-sonnet-4.6, family: claude}
  advisory: {model: gpt-5.4,           family: gpt}
  # per-role override:
  # worker: {model: ..., family: ..., per_tick_timeout: 1800}
loop:
  sleep_secs: 60
  per_tick_timeout: 900       # default per-role timeout
  max_cycles: 0               # 0 = forever
backend: copilot              # only backend currently
```

Edit `crew.yaml` at any time — on the next `run` / `tick`, `agents/*.agent.md` is auto re-rendered from the templates if `crew.yaml` is newer (disable with `--no-auto-render`).

---

## Operator workflow

**Out-of-band nudge a role**

```bash
crewd talk worker "small PRs only — split #42 into 3 PRs"
crewd inbox lead OVERRIDE "drop feature X, focus on auth bug"
```

The role consumes its inbox file at the start of its next tick and moves it to `state/inbox/<role>.processed.<unix-ts>.md` (preserving an audit trail). `OVERRIDE` outranks the role's own plan; `ADVICE` is treated as a strong suggestion; `INFO` is context only.

**Start a new goal on the same workspace**

```bash
$EDITOR GOAL.md          # rewrite the goal
crewd new-goal --from GOAL.md
crewd run                 # lead picks up [OVERRIDE] inbox notice with new label
```

`new-goal` bumps `goal:vN`, closes prior open issues with the previous label, resets cycles, clears `STOPPED`, re-renders `agents/`, and queues an `[OVERRIDE]` line in `lead.md`. Required when reusing a workspace for a fresh goal — otherwise the resumed lead session sees the old `PASS` and writes `STOPPED` immediately.

**Graceful shutdown**

`run` installs `SIGINT` / `SIGTERM` handlers that finish the current tick and then write `state/exit-reason`. Send a second signal to abort hard. Backend itself escalates `SIGINT → SIGTERM → SIGKILL` to copilot subprocesses.

---

## Failure recovery

| Symptom                                                       | Fix                                                                                |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `STOPPED` present at cycle 0 (doctor flags it)                | `crewd resume && crewd run`                                                        |
| Copilot `--continue` fails with `CAPIError 400`               | `mv cfg/<role>/session-state cfg/<role>/session-state.broken-$(date +%s)` then re-run (fresh session). |
| `family check: worker.family == verifier.family`              | Edit `crew.yaml` so they differ; rerun.                                            |
| `target checkout missing`                                     | `crewd attach <owner/repo> --clone`                                                |
| `GOAL.md changed since goal vN started`                       | `crewd new-goal --from GOAL.md` to start a new epoch.                              |
| Lead immediately writes `STOPPED` after restart with new GOAL | You forgot `new-goal`. Run it; verify `state/inbox/lead.md` has `[OVERRIDE]`.      |

---

## Status

- Backend: **GitHub Copilot CLI only** (`gh copilot`).
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
  backends.py           — copilot subprocess + signal escalation
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
```

License: internal / unreleased.
