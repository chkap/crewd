---
name: crewd
description: Operate the crewd multi-agent coding crew CLI — bootstrap a workspace, attach a GitHub repo, run the Lead-directed Lead/Worker/Verifier/Advisory dispatcher, send operator messages via the inbox, swap goals between epochs, and recover from common failures (tainted SDK sessions, family-collision, stuck STOPPED).
---

# crewd skill

> For AI agents operating `crewd` (installed as a CLI; project repo `chkap/crewd`). Read this before issuing any `crewd` command.

## What crewd is

A CLI (`typer` + `jinja2` + `pydantic`; roles run through the official `github-copilot-sdk`) that runs a 4-role autonomous coding crew against a target GitHub repo. Installed on `PATH` as `crewd` (`pipx install crewd`, `uv tool install crewd`, or `pip install crewd`):

- **lead** — plans, schedules, opens umbrella issues. No code, no merges.
- **worker** — writes code, opens PRs. No merges.
- **verifier** — reviews PRs, merges. No code. Two tiers: per-PR + final `crewd:acceptance` gate.
- **advisory** — research, citations, design pointers. No code, no merges.

Each role runs as an official **GitHub Copilot SDK session** with its own `config_directory` set to `cfg/<role>/` (private resumable conversation + config), driven by an `AGENTS.md` file auto-loaded from its working directory at `cfg/<role>/`. Each role has an isolated git worktree at `cfg/<role>/worktree/`. There is **no fixed round-robin**: the **Lead directs the crew** — each cycle it returns one typed decision (`dispatch`/`continue_lead`/`wait`/`pause`/`finish`) and a dispatched role returns one typed handoff (`completed`/`no_progress`), all journaled to a durable SQLite run log for restart-safe recovery. Inter-role communication is GitHub issue/PR comments only. Operator-to-role communication is `state/inbox/<role>.md`.

## When to use this skill

Trigger if the user asks to:

- bootstrap, attach, or restart a crewd workspace
- run / tick / stop / resume the loop
- send an operator message to a role (`talk` / `inbox`)
- swap to a new goal on an existing workspace (`new-goal`)
- diagnose a stuck or failing crew (corrupt session, STOPPED at cycle 0, family collision, immediate STOPPED after goal swap)

## Hard rules — never violate

1. `worker.family ≠ verifier.family`. If they match, `crewd run` exits rc=2. Pick e.g. `gpt` worker + `claude` verifier.
2. Only verifier merges PRs. Only worker writes code. Lead and advisory never touch code or merges.
3. **Pass `-w "$(pwd)"` when the workspace isn't discoverable from cwd.** Installed `crewd` walks up from the current directory for `crew.yaml`, so running inside a workspace needs no flag. Always pass `-w <path>` explicitly when running from elsewhere — and always when using the dev form `uv --directory <crewd-src> run crewd …`, since `--directory` resolves cwd to the crewd source tree and defeats auto-discovery.
4. Reusing an existing workspace for a brand-new goal: **always run `crewd new-goal --from GOAL.md`**. Manually editing `GOAL.md` and rerunning is rejected (`run` exits with sha-mismatch). Even if you delete `goal.json`, the resumed copilot session still remembers the old PASS — `new-goal` is the only path that closes prior issues, resets cycles, queues an `[OVERRIDE]` inbox notice, and re-renders agents.
5. Human/operator blockers use `state/PAUSED`, never repeated idle cycles. Lead must post the exact requested action, pause in the same tick, and leave goal/task issues open.

## Public bus — how roles coordinate on GitHub

Inter-role coordination is **public-first and host-verified**, not model best-effort. Every material message is a real attributed GitHub artifact the host publishes and verifies before authority advances:

- **Attribution.** Every crew-authored comment/PR body starts with one parseable line: `> **[crewd:<role> -> <target>]** <crew-name>`. A body missing/malforming this line is rejected before posting. When you post as a role, keep this exact first line.
- **Idempotency.** Each write carries an invisible `<!-- crewd:correlation:<id> -->` marker. The host reserves a durable intent under `state/public_writes/`, searches for the marker before writing (so a crashed/ambiguous retry never double-posts), writes, then verifies the URL. Intents are `reserved` (post attempted, not yet verified) or `verified`.
- **Restart reconciliation.** Each `crewd run` reconciles reserved-but-unverified intents first — re-scanning for the marker or re-posting idempotently. A landed write is never duplicated; an unreachable GitHub leaves the intent `reserved` and surfaces in `doctor`/`status`, it does not abort the run.
- **Material-before-consume.** A material role handoff (`completed`, or a `no_progress` carrying evidence/changed/remaining/disagreement/blocker) must be a verified public artifact before Lead consumes it; a genuinely empty `no_progress` stays private. Likewise a Lead decision that dispatches Worker/Verifier is published before authority transfers.
- **Prerequisite gating.** Before dispatching Worker/Verifier or before Lead `finish`, the host validates the required GitHub record (active linked `crewd:task`; for finish a closed `crewd:acceptance` + public goal summary). A missing/invalid/unverifiable prerequisite rejects the transition **without** reserving an attempt, consuming a pending handoff, or terminalising — authority never advances on an unverified record.
- **Offline escape hatch.** `CREWD_DISABLE_PUBLIC_BUS=1` runs the dispatcher without the bus (local recovery only); normal attached runs keep it on.

## Standard invocation pattern

```bash
# Installed on PATH (pipx / uv tool / pip). Inside a workspace dir, discovery is
# automatic; pass -w <path> to target a workspace from elsewhere:
crewd <command>                 # from inside the workspace
crewd <command> -w /path/to/workspace
```

Contributor/dev form (running an unreleased crewd from a source checkout) — always pass `-w`:

```bash
uv --directory /path/to/crewd-src run crewd <command> -w "$(pwd)"
```

## Prerequisites (must hold before any run)

- **Python 3.11+**, and `crewd` installed on `PATH`.
- **GitHub Copilot subscription** — roles run as Copilot SDK sessions; no entitlement means no role can run.
- **`gh` authenticated** (`gh auth status`) with **repo read/write** on the target — crewd clones, lists/closes epoch issues, and posts/verifies public-bus comments via `gh`. `git` must also be on `PATH`.
- crewd acts on GitHub **as the authenticated user**: it pushes branches, opens PRs, comments, and (verifier) merges. Scope the token to the target repo; prefer a dedicated bot/fine-grained token.

## Bootstrap a fresh crew (5 steps)

```bash
mkdir -p ~/crews && cd ~/crews
crewd init my-crew --repo owner/target-repo     # clones target into my-crew/repo/
cd my-crew
$EDITOR GOAL.md                                 # write the spec
crewd doctor                                    # must be 0 errors
crewd run --once                                # one dispatcher step (Lead + ≤1 dispatched attempt)
crewd tick lead                                 # debug/compat: force 1 named role, bypassing dispatch
crewd run --daemon                              # loop in background
crewd status                                    # check daemon + crew state
crewd stop                                      # graceful stop (STOPPED + SIGINT)
```

`init` registers the workspace in `~/.crewd/registry.json` so `crewd list` / `crewd cd <name>` work from anywhere.

## Workspace anatomy (paths you'll touch)

```
<workspace>/
├── crew.yaml              ← edit to change models / families / loop
├── GOAL.md                ← spec; do NOT hand-edit after run starts (use new-goal)
├── cfg/<role>/AGENTS.md        ← role instructions (Copilot auto-loads from cwd)
├── cfg/<role>/session-state/   ← Copilot SDK session (config_directory=cfg/<role>; auto-rotates to a fresh generation on taint, see below)
├── cfg/<role>/worktree/        ← git worktree from repo/ (isolated repo copy)
├── state/STOPPED          ← sentinel; loop exits at next check
├── state/PAUSED           ← human/operator action required; goal remains open
├── state/run.pid          ← daemon PID (only when daemon is running)
├── state/cycle.txt        ← legacy mirror
├── state/goal.json        ← {version, label, goal_md_sha256, cycles}
├── state/exit-reason      ← written on graceful exit
├── state/public_writes/   ← durable public-bus intents (reserve→post→verify→reconcile; survive restarts)
├── state/inbox/<role>.md  ← operator → role messages (host-consumed; archived to .processed after delivery)
├── state/logs/<role>/NNNN.log  ← per-attempt output
└── repo/              ← target repo clone (main branch; per-role worktrees in cfg/)
```

## Operator nudges

```bash
# free-form (always shows up the next time the role is run/dispatched):
crewd talk worker "small PRs only — split #42 into 3"

# prioritised (preferred for machine-readable directives):
crewd inbox lead OVERRIDE "drop feature X; focus on auth bug"
crewd inbox advisory ADVICE "investigate https://example.com/postmortem"
crewd inbox verifier INFO "expect a doc-only PR shortly"
```

Priorities: `OVERRIDE > ADVICE > INFO`. The **host** consumes the inbox when it builds the role's next dispatched prompt — it stages the live `<role>.md` (plus any orphaned staging from a crashed prior attempt) to `<role>.delivering.<attempt>.md`, injects the messages inline (highest priority first), and archives to `state/inbox/<role>.processed.<ts>.md` only **after that attempt durably terminalises**. A crash before acknowledgement retains the message for redelivery to the next attempt, so messages don't pile up and an `OVERRIDE` can't be lost or silently skipped by the model.

## Goal lifecycle (epochs)

The crew can chew through one or more "goals" on the same workspace. Each goal is an **epoch** with an integer version and a GitHub label `goal:vN` applied to all of its issues.

To start a new epoch on an existing workspace:

```bash
$EDITOR GOAL.md          # rewrite the spec in-place
crewd new-goal --from GOAL.md
crewd run                # lead picks up the [OVERRIDE] inbox notice
```

What `new-goal` does, in order:

1. Bumps `state/goal.json` to `version+1`, label `goal:v<N+1>`.
2. Closes all open issues bearing the prior `goal:vN` label.
3. Clears `state/STOPPED`, `state/PAUSED`, and `state/exit-reason`; resets `cycles` to 0.
4. Re-renders `agents/*.agent.md` with the new label.
5. Appends `## [OVERRIDE @ <ts>]` to `state/inbox/lead.md`.

Skip `new-goal` and the resumed lead session will see the old `PASS` and write `STOPPED` instantly.

## Doctor — read this first when something's wrong

```bash
crewd doctor -w "$(pwd)"
```

It prints: roles table (models / families / agent.md freshness / session-state / last-log), state table (STOPPED, PAUSED/reason, cycle), inbox table (pending / delivering / processed counts per role), a public-writes table (verified / reserved-unverified intents; warns that `crewd run` reconciles pending ones once GitHub is reachable), recent activity, and a `suggestions:` list. Anything tagged `ERROR` blocks `run` (rc=1).

## Recovery cookbook

| Symptom                                                          | Recovery                                                                                                |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `STOPPED` present at cycle 0                                     | `crewd resume && crewd run`                                                                             |
| SDK session resume fails / dirty session          | Rare — a tainted session auto-advances to a fresh generation on the next run. To force it, `mv cfg/<role>/session-state cfg/<role>/session-state.broken-$(date +%s)` then re-run. |
| `family check: worker.family == verifier.family`                 | Edit `crew.yaml` so families differ (e.g. gpt vs claude). Auto re-render handles agents/.               |
| `target repo clone missing`                                      | `crewd attach <owner/repo> --clone`                                                                     |
| `GOAL.md changed since goal vN started`                          | `crewd new-goal --from GOAL.md` (don't bypass — see Hard rule #4).                                      |
| Lead writes `STOPPED` immediately after restart with new GOAL    | You forgot `new-goal`. Run it; confirm `state/inbox/lead.md` ends with `[OVERRIDE]`.                    |
| `PAUSED` with `human-blocked:` reason                            | Resolve the stated action, then run `crewd resume` and restart the daemon.                              |
| Want to kill the loop cleanly                                    | `crewd stop` (writes STOPPED + sends SIGINT to daemon). `crewd stop --force` sends SIGKILL.             |
| `backend: migration required` / `backend: copilot ... removed`   | Legacy workspace on the retired subprocess backend. `crewd refresh` migrates `crew.yaml` to `backend: copilot-sdk` (preserving unknown config keys + STOPPED/PAUSED/goal.json/session-state/public_writes), then `crewd doctor`. Idempotent. |
| `extra_add_dirs entry ... resolves outside the workspace`        | External/symlink-to-external context is a non-runnable blocker (SDK has no `--add-dir`; single working_directory). `doctor` errors and `run` pre-flight refuses (rc=2) **before** any backend/dispatch/SDK work. Copy/sanitize only the needed context into the workspace and repoint `extra_add_dirs` at that in-workspace path. |
| `public write(s) reserved but unverified` (doctor/status)        | A crash left a public-bus intent mid-post. `crewd run` reconciles it against GitHub (idempotent — no duplicate comment) once GitHub is reachable. If it persists, check GitHub auth/connectivity; do not hand-edit `state/public_writes/`. |

## Graceful shutdown semantics

`run` (foreground or `--daemon`) installs `SIGINT`/`SIGTERM` handlers that flip an interrupt flag — the current attempt finishes, then `state/exit-reason` is written and the loop exits 0. `crewd stop` writes the `STOPPED` sentinel and sends `SIGINT` to the daemon PID if running; `--force` sends `SIGKILL`. Mid-attempt cancellation is a single non-blocking `CancelToken` abort of the in-flight SDK session; if the abort cannot be confirmed idle the session is tainted and force-stopped, so the next run starts a fresh generation instead of resuming a dirty session. A second signal aborts hard.

When Lead writes `state/PAUSED`, the loop stops before the next role and records
`exit-reason: human-blocked`. This is resumable and deliberately distinct from
`STOPPED`/`goal-complete`; do not poll a known human blocker with more crew cycles.

## What NOT to do

- ❌ Don't hand-edit `agents/*.agent.md` — they're regenerated from `crew.yaml`. Edit the Jinja templates in `src/crewd/templates/agents/` instead, then `crewd run` (or `crewd doctor` to spot stale agent.md).
- ❌ Don't write to `state/inbox/<role>.md` directly — use `crewd talk` / `crewd inbox` so the timestamp + format are correct.
- ❌ Don't share workspace files between roles as a comm channel. Use GitHub issues / PR comments. The roles are explicitly told that `state/` is private.
- ❌ Don't `rm -rf cfg/<role>/session-state` to "fix" things — **rename** it instead (`*.broken-<ts>`) so you can post-mortem.
- ❌ Don't run two `crewd run` processes against the same workspace.

## Files to read for deeper changes

- `src/crewd/cli.py` — typer entrypoint (all commands)
- `src/crewd/commands.py` — implementations (init, run loop, new_goal, doctor)
- `src/crewd/config.py` — pydantic schema for `crew.yaml` and `GoalState`
- `src/crewd/workspace.py` — path layout + STOPPED sentinel
- `src/crewd/orchestrator.py` — Lead-directed run loop (cancellable attempts, pre-send journal identity)
- `src/crewd/dispatcher.py` — durable SQLite run journal (dispatch/attempt/handoff/solicitation)
- `src/crewd/executor.py` — runs one role/lead attempt → typed handoff/decision
- `src/crewd/sdk_adapter.py` — official Copilot SDK runtime + typed tools
- `src/crewd/session_backend.py` — session registry, goal-scoped ids/generations, CancelToken, taint store
- `src/crewd/diagnostics.py` — read-only operator status surface
- `src/crewd/backends.py` — thin SdkBackend exit-code adapter (retires legacy `copilot`)
- `src/crewd/templates/agents/*.j2` — role prompts (UX lens, two-tier verifier, Final Acceptance Gate)

## Verification after any change

Contributor check from a crewd **source checkout** (not needed for installed use):

```bash
uv run pytest -q                 # from the crewd source tree
# then in a throwaway workspace (installed CLI, or the dev `uv --directory` form):
crewd init /tmp/crewd-smoke --repo owner/dummy
crewd doctor -w /tmp/crewd-smoke
```
