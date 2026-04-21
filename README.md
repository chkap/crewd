# crewd

> Multi-agent coding crew CLI — workspace-based, GitHub Issues as the bus, round-table loop scheduler.

`crewd` packages the **Lead / Worker / Verifier / Advisory** pattern (proven on rocket-edu-mvp) into a reusable CLI. One workspace per crew, attachable to any GitHub repo. Each role runs as a GitHub Copilot CLI session with its own `--config-dir` and a continued conversation across cycles.

## Quickstart

```bash
# 1. Init a workspace (separate from your target repo)
mkdir ~/crews && cd ~/crews
crewd init my-crew --repo myorg/my-app

# 2. Edit the goal
crewd goal --edit -w my-crew

# 3. Sanity check
crewd doctor -w my-crew

# 4. Run one cycle in foreground
crewd run -w my-crew --once
```

## Workspace layout
```
my-crew/
  crew.yaml              # config
  GOAL.md                # current goal/spec
  agents/                # per-role .agent.md (role + responsibilities)
    lead.agent.md
    worker.agent.md
    verifier.agent.md
    advisory.agent.md
  state/
    STOPPED              # sentinel: loop exits
    cycle.txt
    logs/<role>/<NNNN>.log
  cfg/                   # per-role copilot --config-dir
    lead/  worker/  verifier/  advisory/
  checkout/              # cloned target repo
```

## Hard rules baked in
- **Worker.family ≠ Verifier.family** (enforced at startup; refuses to run if violated)
- Worker writes code, never approves PRs
- Verifier approves and merges PRs, never writes code
- Lead plans and schedules, never writes code, never approves PRs
- Advisory cites sources, never writes code or approves anything
- All cross-role communication happens via GitHub issue/PR comments

## Status
**Phase 1**: scaffolding, `init` / `attach` / `doctor` / `goal` / `run --once` / `status` / `stop`. Backend: GitHub Copilot CLI only.
