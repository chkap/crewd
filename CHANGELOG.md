# Changelog

All notable, user-facing changes to crewd are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and crewd
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] — 2026-07-31

Documentation and behavior refinements on top of 0.1.0. This is a
**backward-compatible** release: `crew.yaml`, workspace layout, the durable run
journal, and the `crewd` CLI surface are unchanged, so upgrading is a drop-in
`pip install --upgrade crewd` followed by `crewd refresh` + `crewd doctor` in each
workspace (durable state and unknown config keys are preserved; the journal
schema migrates in place idempotently). No configuration or command changes are
required.

### Changed

- **Lead-directed dispatch is exact-bound and host-governed.** Each `dispatch` binds one role
  to one specific `crewd:task` with a correlation id; the attempt, handoff, and public comment
  carry that exact binding so authority advances only on the record the Lead addressed.
- **Typed, intent-aware gate corrections.** A dispatch carries a typed intent (implementation,
  verifier audit, acceptance, release, advisory); the prerequisite gate validates the right
  record for that intent and corrects a rejected transition in place instead of consuming an
  attempt or terminalising the run.
- **Bounded WAIT vs operator-only PAUSE, with separated recovery budgets.** Transient
  GitHub/SDK **transport** failures, **uncertain**/missing/malformed decisions, and ordinary
  **no-progress** now carry independent durable budgets and leave the run `WAITING` with an
  observable wake condition that self-heals on the next reconcile. `PAUSE` is reserved for a
  genuine operator-only prerequisite, so the crew no longer raises a false human blocker for
  internal retries or transient errors.
- **Durable evidence recovery.** When a role attempt terminates without delivering its
  structured handoff, the host performs an exact-bound search of the GitHub record (branch/PR/
  checks) and reconciles that evidence rather than discarding real work.
- **High-leverage Advisory consultation policy.** When Advisory is configured, Lead consults it
  before consequential decisions (not on a fixed rotation), records a one-line reason when it
  skips consultation for a routine step, and treats Advisory input as proactive but non-binding.

### Removed

- **Model-selected `continue_lead` decision.** The Lead decision contract no longer exposes a
  "keep going" outcome; the host manages re-solicitation, interruption, and timeout recovery
  under bounded per-class budgets. Lead uses `wait` (with a wake condition) to request another
  planning turn.

## [0.1.0] — 2026-07-30

First public release of crewd — a multi-agent coding-crew CLI that runs a
**Lead / Worker / Verifier / Advisory** crew against a target GitHub repository,
coordinating entirely through GitHub Issues and Pull Requests.

### Added

- **SDK-native role backend (`backend: copilot-sdk`).** Every role runs as an
  official GitHub Copilot SDK session with its own resumable `config_directory`
  and an auto-loaded `AGENTS.md`. This is the default and only production
  backend; the legacy `copilot -p` subprocess transport is retired and rejected
  at pre-flight with a migration message.
- **Lead-directed dispatch (no fixed round-robin).** Each cycle the Lead is
  solicited and returns exactly one typed decision — `dispatch`, `continue_lead`,
  `wait`, `pause`, or `finish`. A dispatched role runs a single attempt and
  returns exactly one typed handoff (`completed` / `no_progress`) that feeds the
  Lead's next decision. Only the worker writes code; only the verifier merges,
  behind a two-tier review plus a Final Acceptance Gate.
- **Durable GitHub public bus.** Every material inter-role message — each Lead
  dispatch decision and each role handoff — is a real, attributed GitHub
  comment (`> **[crewd:<role> -> <target>]** <crew>`) that the host publishes
  and verifies before authority advances. Writes carry an invisible correlation
  marker and a reserve → search → write → verify lifecycle, so a crash or an
  ambiguous retry never double-posts. A material handoff must be a verified
  public artifact before Lead may consume it. `CREWD_DISABLE_PUBLIC_BUS=1` runs
  the dispatcher offline for local recovery.
- **Prerequisite gating.** Before dispatching Worker/Verifier and before a Lead
  `finish`, the bus validates the required GitHub record (active linked
  `crewd:task`; for finish, a closed `crewd:acceptance` issue plus a public goal
  summary). A missing/invalid/unverifiable prerequisite rejects the transition
  without reserving an attempt, consuming a handoff, or terminalising the run.
- **Operator inbox with host-owned delivery.** `crewd talk` / `crewd inbox`
  queue prioritised (`OVERRIDE` > `ADVICE` > `INFO`) messages the host injects
  into a role's next dispatched prompt under an `OPERATOR INBOX` banner. Delivery
  is a two-phase, at-least-once handshake: a crash before acknowledgement retains
  the message for redelivery, so an `OVERRIDE` is never lost or consumed by the
  wrong role.
- **Restart-safe recovery.** A durable SQLite run journal records every
  dispatch, attempt, handoff, and solicitation for at-least-once, idempotent
  recovery. On each `crewd run`, orphaned in-flight attempts are reconciled and
  reserved-but-unverified public writes are finalized (idempotently) before new
  work. Graceful `SIGINT`/`SIGTERM` shutdown finishes the current attempt;
  mid-attempt cancellation taints and force-stops a session that cannot confirm
  idle so the next run starts a fresh generation.
- **WAIT vs PAUSE recovery classification.** A transient GitHub failure or a
  closed-target ordering race leaves the run `WAITING` and self-heals on the next
  run's reconcile; only a genuine credential/permission/policy blocker `PAUSES`
  for a human. `crewd doctor` / `crewd status` distinguish self-healing pending
  writes from those that genuinely need an operator.
- **Workspace lifecycle & migration.** `crewd init` scaffolds and registers a
  workspace; `crewd attach` clones the target repo and creates per-role git
  worktrees; `crewd refresh` re-renders agents and migrates a legacy workspace
  (including `backend: copilot` → `backend: copilot-sdk`) while preserving
  unknown config keys and durable state; `crewd new-goal` starts a new `goal:vN`
  epoch (closes prior-label issues, resets cycles, queues `[OVERRIDE]` notices).
- **Operator diagnostics.** `crewd doctor` and `crewd status` provide read-only
  snapshots (roles, families, state, inbox counts, public-write intents, recent
  activity, and a safe next action) with bounded secret redaction of durable
  logs.
- **Public package contract.** MIT licensed (SPDX metadata + bundled `LICENSE`);
  a single authoritative version (`crewd.__version__`, reported by `crewd
  --version` and consumed by the build so package metadata cannot drift);
  complete PyPI metadata (description, authors, keywords, project URLs,
  `requires-python >=3.11`) with Python 3.11–3.14 verified by the build-once
  release CI. The supported public contract is the `crewd` CLI, not an
  importable typed API (no `py.typed`).
- **Secure release publishing.** A dedicated `publish.yml` workflow releases to
  PyPI via **Trusted Publishing (OIDC)** — no long-lived API token. It triggers
  only on a published GitHub Release, validates tag/version/commit provenance,
  builds the wheel+sdist once, re-runs the Python 3.11–3.14 clean-install
  acceptance on those exact artifacts, and publishes them from the protected
  `pypi` environment with least privilege (`id-token: write` on the publish job
  only) and idempotent, duplicate-safe retries. See
  [`docs/releasing.md`](docs/releasing.md).

### Known limitations

- Coordination is **GitHub-only**; there is no other-forge backend.
- The **Copilot SDK is the only backend** and requires a GitHub Copilot
  subscription.
- The SDK mounts a **single working directory** (the workspace root) with no
  `--add-dir` equivalent, so all role-visible context must live in the workspace.
- Run **one `crewd run` per workspace**.
- Primarily exercised on **Linux**; other platforms are unvalidated.

[Unreleased]: https://github.com/chkap/crewd/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/chkap/crewd/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/chkap/crewd/releases/tag/v0.1.0
