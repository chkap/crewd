---
name: crewd
description: Operate the crewd multi-agent coding crew CLI — bootstrap a workspace, attach a GitHub repo, run/tick the Lead/Worker/Verifier/Advisory loop, send operator messages via the inbox, swap goals between epochs, and recover from common failures (corrupt copilot sessions, family-collision, stuck STOPPED).
---

# crewd skill

> For AI agents operating `crewd` (`~/crewd`, repo `chkap/crewd`). Read this before issuing any `crewd` command.

## What crewd is

A CLI (`uv` + `typer` + `jinja2` + `pydantic`) that runs a 4-role autonomous coding crew against a target GitHub repo:

- **lead** — plans, schedules, opens umbrella issues. No code, no merges.
- **worker** — writes code, opens PRs. No merges.
- **verifier** — reviews PRs, merges. No code. Two tiers: per-PR + final `crewd:acceptance` gate.
- **advisory** — research, citations, design pointers. No code, no merges.

Each role is a `gh copilot` subprocess with its own `--config-dir` (private resumable conversation), driven by an `AGENTS.md` file auto-loaded from its working directory at `cfg/<role>/`. Each role has an isolated git worktree at `cfg/<role>/worktree/`. Inter-role communication is GitHub issue/PR comments only. Operator-to-role communication is `state/inbox/<role>.md`.

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
3. **Always pass `-w "$(pwd)"`** when invoking via `uv --directory ~/crewd run crewd …` from inside a workspace. `uv --directory` resolves cwd to the crewd repo, so workspace auto-discovery picks the wrong directory otherwise.
4. Reusing an existing workspace for a brand-new goal: **always run `crewd new-goal --from GOAL.md`**. Manually editing `GOAL.md` and rerunning is rejected (`run` exits with sha-mismatch). Even if you delete `goal.json`, the resumed copilot session still remembers the old PASS — `new-goal` is the only path that closes prior issues, resets cycles, queues an `[OVERRIDE]` inbox notice, and re-renders agents.

## Standard invocation pattern

```bash
# Inside a workspace dir:
uv --directory ~/crewd run crewd -w "$(pwd)" <command>
```

If you've installed `crewd` on PATH (e.g. `uv pip install -e ~/crewd`), you can drop the `uv --directory` prefix and rely on git-style upward `crew.yaml` discovery — but the safe form above always works.

## Bootstrap a fresh crew (5 steps)

```bash
mkdir -p ~/crews && cd ~/crews
uv --directory ~/crewd run crewd init my-crew --repo owner/target-repo
cd my-crew
$EDITOR GOAL.md                                                # write the spec
uv --directory ~/crewd run crewd -w "$(pwd)" doctor            # must be 0 errors
uv --directory ~/crewd run crewd -w "$(pwd)" tick lead         # smoke-test 1 role
uv --directory ~/crewd run crewd -w "$(pwd)" run --once        # one full cycle
uv --directory ~/crewd run crewd -w "$(pwd)" run --daemon     # loop in background
uv --directory ~/crewd run crewd -w "$(pwd)" status           # check daemon + crew state
uv --directory ~/crewd run crewd -w "$(pwd)" stop             # graceful stop (STOPPED + SIGINT)
```

`init` registers the workspace in `~/.crewd/registry.json` so `crewd list` / `crewd cd <name>` work from anywhere.

## Workspace anatomy (paths you'll touch)

```
<workspace>/
├── crew.yaml              ← edit to change models / families / loop
├── GOAL.md                ← spec; do NOT hand-edit after run starts (use new-goal)
├── cfg/<role>/AGENTS.md        ← role instructions (Copilot auto-loads from cwd)
├── cfg/<role>/session-state/   ← copilot --config-dir (rotate on corruption, see below)
├── cfg/<role>/worktree/        ← git worktree from repo/ (isolated repo copy)
├── state/STOPPED          ← sentinel; loop exits at next check
├── state/run.pid          ← daemon PID (only when daemon is running)
├── state/cycle.txt        ← legacy mirror
├── state/goal.json        ← {version, label, goal_md_sha256, cycles}
├── state/exit-reason      ← written on graceful exit
├── state/inbox/<role>.md  ← operator → role messages (consumed + moved to .processed by role)
├── state/logs/<role>/NNNN.log  ← per-tick output
└── repo/              ← target repo clone (main branch; per-role worktrees in cfg/)
```

## Operator nudges

```bash
# free-form (always shows up in role's next tick prompt):
crewd talk worker "small PRs only — split #42 into 3"

# prioritised (preferred for machine-readable directives):
crewd inbox lead OVERRIDE "drop feature X; focus on auth bug"
crewd inbox advisory ADVICE "investigate https://example.com/postmortem"
crewd inbox verifier INFO "expect a doc-only PR shortly"
```

Priorities: `OVERRIDE > ADVICE > INFO`. The role consumes the inbox at the start of its next tick and moves it to `state/inbox/<role>.processed.<unix-ts>.md` (audit trail), so messages don't pile up.

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
3. Clears `state/STOPPED` and `state/exit-reason`; resets `cycles` to 0.
4. Re-renders `agents/*.agent.md` with the new label.
5. Appends `## [OVERRIDE @ <ts>]` to `state/inbox/lead.md`.

Skip `new-goal` and the resumed lead session will see the old `PASS` and write `STOPPED` instantly.

## Doctor — read this first when something's wrong

```bash
crewd -w "$(pwd)" doctor
```

It prints: roles table (models / families / agent.md freshness / session-state / last-log), state table (STOPPED, cycle), inbox table (pending count + last sender per role), recent activity, and a `suggestions:` list. Anything tagged `ERROR` blocks `run` (rc=1).

## Recovery cookbook

| Symptom                                                          | Recovery                                                                                                |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `STOPPED` present at cycle 0                                     | `crewd resume && crewd run`                                                                             |
| Copilot `--continue` fails with `CAPIError 400` (orphan tool_use) | `mv cfg/<role>/session-state cfg/<role>/session-state.broken-$(date +%s)` then re-tick (fresh session). |
| `family check: worker.family == verifier.family`                 | Edit `crew.yaml` so families differ (e.g. gpt vs claude). Auto re-render handles agents/.               |
| `target checkout missing`                                        | `crewd attach <owner/repo> --clone`                                                                     |
| `target repo clone missing`                                      | `crewd attach <owner/repo> --clone`                                                                     |
| `GOAL.md changed since goal vN started`                          | `crewd new-goal --from GOAL.md` (don't bypass — see Hard rule #4).                                      |
| Lead writes `STOPPED` immediately after restart with new GOAL    | You forgot `new-goal`. Run it; confirm `state/inbox/lead.md` ends with `[OVERRIDE]`.                    |
| Want to kill the loop cleanly                                    | `crewd stop` (writes STOPPED + sends SIGINT to daemon). `crewd stop --force` sends SIGKILL.             |

## Graceful shutdown semantics

`run` (foreground or `--daemon`) installs `SIGINT`/`SIGTERM` handlers that flip an interrupt flag — the current tick finishes, then `state/exit-reason` is written and the loop exits 0. `crewd stop` writes the `STOPPED` sentinel and sends `SIGINT` to the daemon PID if running; `--force` sends `SIGKILL`. The backend escalates child copilot processes `SIGINT → SIGTERM → SIGKILL` (SIGINT preferred — copilot has a dedicated handler that flushes events.jsonl cleanest, avoiding the orphan tool_use that breaks `--continue`).

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
- `src/crewd/backends.py` — copilot subprocess + signal escalation
- `src/crewd/templates/agents/*.j2` — role prompts (UX lens, two-tier verifier, Final Acceptance Gate)

## Verification after any change

```bash
cd ~/crewd && uv run pytest -q
# then in a throwaway workspace:
uv --directory ~/crewd run crewd init /tmp/crewd-smoke --repo owner/dummy
uv --directory ~/crewd run crewd -w /tmp/crewd-smoke doctor
```
