# Orchestration Retrospective — evidence baseline for the SDK-native, Lead-directed refactor

> Status: durable evidence baseline for GOAL.md and issues #10–#13. Read this before
> designing the SDK backend (#10), the Lead-directed dispatcher (#11), the role prompts
> (#12), or the compatibility/observability/recovery work (#13).

This retrospective derives orchestration requirements from the recorded run history of two
prior crews (`fin-crew`, `legal-crew`). It is deliberately evidence-led: every material
design conclusion is tied to a countable pattern or a citable log trace, and uncertainty is
stated where the logs cannot settle a question.

**All historical directories are read-only.** No credentials, environment files, inboxes,
session-state, or personal data were inspected or are reproduced here. Only per-role tick
logs under `state/logs/goal-v*/<role>/NNNN.log` and the aggregate `daemon.log` were read.

---

## 1. What the histories are

| Crew | Goals present | Lead ticks | Advisory | Worker | Verifier | Total role logs |
|------|---------------|-----------:|---------:|-------:|---------:|----------------:|
| fin-crew | v1–v13 | 1,503 | 1,491 | 1,489 | 1,488 | 5,971 |
| legal-crew | v1 only | 161 | 149 | 147 | 146 | 603 |

The provided sanitized summaries are orientation only and disagree with the logs
(`fin-crew-summary.txt` says "v1 / 160 cycles" but the logs span v1–v13 with ~1,502 cycles;
`legal-crew-summary.txt` says "v13 / 22" but the logs contain only goal v1 with ~159–161
cycles). **Treat the summaries as context, not as denominators.** All rates below use the
log-derived counts.

fin-crew is dominated by two long steady-state epochs: **goal-v7 = 1,102 cycles** and
goal-v8 = 246 cycles. legal-crew is a single shorter, infrastructure-heavy epoch. The two
crews are complementary: fin exposes **steady-state fixed-loop waste**, legal exposes
**failure/recovery and human-intervention churn**.

---

## 2. Sampling method (reproducible)

A reviewer can reproduce every number here.

**Stage A — full census (primary quantitative layer).** Rather than sample, all 6,574 role
logs were parsed programmatically. For each log we extracted the footer (`Changes +a -b`,
`AI Credits`, `Resume`) and matched a fixed keyword rubric (timeout / signal / cancel /
paused / stopped / stale / resume / no-PR / waiting / no-progress / merge / pr-ready /
error / crash). Counts in this document are exact over the whole population, so there is no
sampling error in the rates — only classifier imprecision (Stage C).

**Stage B — exhaustive exception census.** Every timeout/SIGINT/SIGTERM/SIGKILL,
cancellation, and log-numbering gap was enumerated and the boundary cycles read in full
(see §5). Rare failures are reported as counts, never extrapolated to rates.

**Stage C — deterministic validation subsample.** To measure how well the keyword rubric
approximates *semantic* productivity, a seeded random subsample was hand-read
(`random.seed(1729)`, 16 logs). This calibrates the "idle" numbers and exposes their
limits (§4). The seed, the selection algorithm, and the sampled paths are all reproducible
from the description above; the parser matched every log (6,573/6,574 had a footer; the one
without was a SIGKILLed tick, itself a finding).

**Coding rubric for a tick** (per Advisory guidance, to avoid inferring value from tool
calls or runtime): each tick is `productive` (artifact/routing change: PR opened/merged,
issue assigned, changes requested, code pushed), `idle` (explicit no-op / waiting / no open
work), `mixed`, or `unclear`. **Value is never inferred from a nonzero diff, nonzero
runtime, exit 0, or issue activity alone** — those oracles are shown unreliable in §3.

---

## 3. Why the obvious oracles are unreliable (measurement caveats)

These caveats gate how the later tasks must report observability (#13).

- **Invocation count ≠ productivity.** Advisory ran in 1,491/1,503 fin cycles (99.2%);
  Worker and Verifier are similarly near-universal. This measures the *fixed schedule*, not
  usefulness — it is exactly the pattern the refactor must break.
- **The diff footer is cumulative worktree noise, not per-tick output.** fin goal-v7 worker
  cycle 0325 is an explicit *idle* tick ("no open `crewd:task` issues, no open PRs") yet its
  footer reads `Changes +28502 -1111`. The number reflects accumulated worktree state on a
  resumed session, not work done this tick.
- **`AI Credits` and the elapsed time are cumulative per resumed session, not per tick.**
  The same idle cycle 0325 footer shows `398h 42m` / thousands of credits — the running
  total of a persistent `copilot --resume` session. **Per-tick latency cannot be recovered
  from these logs**; handoff/rework delay must therefore be discussed in *cycles*, not
  wall-clock, and #13 must emit real per-tick timing that these logs never captured.
- **Sentinel keywords over-fire.** `STOPPED` appears in 95% of fin lead logs — almost
  entirely from the routine per-tick shell command that checks `state/PAUSED` and
  `state/STOPPED`, not from actual stoppage. Keyword presence must be corroborated with the
  surrounding narration.

**Architectural fact established by the census:** 100% of role logs invoke
`copilot --continue` / `--resume`. Every role is a **persistent CLI subprocess resumed each
tick** — precisely the manual `copilot -p` supervision GOAL.md replaces with SDK sessions.

---

## 4. Finding 1 — the fixed round-robin spends most invocations on no-op ticks

The current loop (`commands.py::_LoopController.loop`) walks
`ROLES = (lead, advisory, worker, verifier)` in fixed order **every cycle**, ticking each
configured role once whether or not it has work.

Hand-calibrated idle classification over the full census:

| Crew / role | idle | productive | mixed | unclear | Notes |
|-------------|-----:|-----------:|------:|--------:|-------|
| fin / worker | 93% | 3% | 4% | 0% | steady-state: almost never any assigned task |
| fin / advisory | 90% | 0% | 0% | 9% | invoked with nothing to advise on |
| fin / lead | 74% | 2% | 1% | 23% | most "unclear" validated as idle |
| fin / verifier | 5% | 4% | 1% | 89% | "unclear" = terse "No new activity. Standing down" — idle |
| legal / worker | 10% | 47% | 31% | 12% | genuinely busy infra project |
| legal / verifier | 33% | 22% | 32% | 13% | real review/rework flow |
| legal / lead | 19% | 16% | 11% | 55% | active routing + overrides |

**Stage-C validation** (seed 1729) confirmed the classifier *under*-counts idle: logs it
marked `unclear` were, on reading, idle no-ops — e.g. fin v7 `lead/0081` ("No new inbox or
GitHub updates changed the plan"), v7 `verifier/0383` ("No new activity. Standing down cycle
383"). So the true fin idle fraction is **higher** than the table's idle column.

**Conclusion (high confidence).** In the largest history (fin, ~6,000 invocations,
overwhelmingly the long steady-state v7 epoch), the fixed loop burned the large majority of
role invocations on repeated no-op status checks — each still paying a full model round-trip.
legal, a short high-activity epoch, is the opposite: roles were mostly busy. **A fixed
schedule is only wasteful in proportion to idle time, and both extremes appear in the data.**
This is the central case for Lead-directed dispatch (#11): invoke a role only when there is
plausible work for it.

---

## 5. Finding 2 — failure, cancellation, and stale-session behavior

legal-crew (infra-heavy, real external calls) is where lifecycle behavior is visible.
Exception census counts (Stage B, exact):

| Signal (log mentions) | legal worker (n=147) | fin worker (n=1,489) |
|-----------------------|---------------------:|---------------------:|
| timeout / rc=124 | 37 (25%) | 48 (3%) |
| SIGINT/SIGTERM/SIGKILL | 14 (10%) | 33 (2%) |
| cancel | 12 (8%) | 35 (2%) |
| crash/killed/OOM terms | 22 (15%) | 5 (0%) |
| error/exception/traceback | 44 (30%) | 62 (4%) |
| `AI Credits 0 (0s)` cancelled-tick | 12 (8%) | 33 (2%) |

**Case study — legal goal-v1 cycle 77 (the decisive trace).**
1. Worker ran the full budget and hit `[crewd] TIMEOUT after 1800s`; the backend escalated
   `SIGINT (grace 20s)` → *ignored* → `SIGTERM (grace 10s)` → *ignored* →
   `SIGKILL (session MAY corrupt)`. The tick never printed a footer (the one
   footer-less log in the census).
2. **Verifier cycle 77 still ran** in fixed order and reported an idle waiting state
   ("No open PRs … #21 remains open, waiting on Worker"). The next *useful* action after a
   forced-killed Worker was not "run Verifier"; it was to handle the failed attempt / return
   to Lead. The fixed loop instead spent a wasted Verifier invocation.
3. **`worker/0078.log` is missing** — the cycle immediately after the SIGKILL skipped Worker
   entirely. A concrete artifact of degraded/tainted-session handling with no recorded
   reason.
4. Lead cycle 78 then took a governance override and re-routed (added a
   `blocked:owner-approval` label) — a *material* intervention that changed the next action,
   but only after the wasted Verifier tick and the fixed-order delay.

**Backend behavior today** (`backends.py::CopilotBackend.run_role`): after `SIGKILL` it logs
"session MAY corrupt" and returns, and the **next tick simply `--continue`s the same session
regardless** — a forced kill silently resumes a possibly-corrupt session rather than tainting
it. fin goal-v12 (`lead=1, advisory=1, worker=0, verifier=0`) is a second stale-state
artifact: an epoch that produced one Lead + one Advisory tick and then nothing, with no
terminal marker distinguishing "aborted" from "finished".

**Conclusion (high confidence on lifecycle implications).** Forced termination and normal
cancellation are today indistinguishable from a healthy resume; terminal/empty states are
ambiguous; and a failed attempt does not return control to Lead. These are the recovery
requirements for #10/#11/#13.

---

## 6. Finding 3 — repeated discovery/status work every tick

Nearly every log begins with the same boilerplate: read inbox, check `state/PAUSED` +
`state/STOPPED`, then `gh issue list` and `gh pr list`. Because every role does this every
cycle, a large share of each idle tick's cost is **re-discovering unchanged GitHub state**.
The 95% `STOPPED` keyword rate on fin lead logs is this poll, not real stoppage. Lead-directed
dispatch plus a compact shared handoff (#11/#12) should let a role receive the state delta it
needs instead of re-querying the whole board from scratch each tick.

---

## 7. Finding 4 — when Advisory and Lead intervention actually mattered

- **Advisory value is bimodal.** In fin steady state it was ~90% idle (invoked by schedule
  with nothing to review). Its high-value contributions were concentrated at *decision
  boundaries* — e.g. this very refactor's issue #9, where Advisory's census corrected the
  summary denominators and re-scoped #10–#13. Requirement: Advisory should be dispatched on
  demand at decision/uncertainty points, not every cycle.
- **Lead intervention changed the next action** exactly at exception boundaries (legal cy78
  override) and epoch transitions, not during steady state. Requirement: the dispatcher must
  give Lead the first move after any non-Lead completion that is failed / cancelled /
  no-progress, so interventions are not delayed behind fixed-order ticks.

---

## 8. Derived requirements (testable) for #10–#13

Each requirement cites the evidence above. "Testable" = a reviewer can write an assertion or
reproduce a trace.

### Orchestration / dispatch (#11)
- **R1.** Replace fixed `for r in ROLES` with Lead-selected next action; a role with no
  plausible work is not invoked. *Evidence:* §4 idle rates. *Test:* given no open task and
  no PR, a tick schedules no Worker/Verifier invocation.
- **R2.** Any non-Lead completion that is `failed`, `cancelled`, or `no-progress` returns
  control to Lead rather than proceeding to a precomputed next role. *Evidence:* §5 legal
  cy77 wasted Verifier. *Test:* a timed-out Worker attempt routes to Lead next, not Verifier.
- **R3.** Persist routing decisions + run/attempt identity so a restart cannot lose the next
  action or mis-read a terminal marker. *Evidence:* §5 fin v12 ambiguous empty epoch;
  cumulative-session confusion in §3. *Test:* kill mid-cycle, restart, and the same pending
  dispatch resumes.

### SDK backend / lifecycle (#10)
- **R4.** Forced termination must **taint** a session (distinct observable outcome) rather
  than silently `--continue` it. *Evidence:* §5 "session MAY corrupt" then blind resume.
  *Test:* after SIGKILL, resuming the same session is refused/flagged, not transparent.
- **R5.** Cancellation escalation, timeout, disconnect, and normal idle-completion must have
  **distinct, recorded outcomes** (not all collapsing to "returned"). *Evidence:* §3 (exit
  codes/duration uninformative), §5 counts. *Test:* each path yields a distinct outcome enum.
- **R6.** Provide real per-tick timing and per-tick work accounting from the SDK event
  stream. *Evidence:* §3 cumulative footers make per-tick latency unrecoverable. *Test:* two
  consecutive resumed ticks report independent (non-cumulative) durations.

### Handoff contract / prompts (#12)
- **R7.** The structured handoff must carry an explicit **outcome class** (`productive` /
  `idle-no-progress` / `blocked` / `failed`) plus evidence references, so
  unchanged-state/no-progress is reported rather than inferred from a diff. *Evidence:* §3
  diff unreliability, §4 idle mislabeled by naive oracles. *Test:* an idle Worker tick emits
  `idle-no-progress`, and the dispatcher does not then invoke Verifier.
- **R8.** Prompts drop fixed-cycle assumptions (no "every tick run all roles"); each role
  states what fresh delta to read and when control returns to Lead. *Evidence:* §6 repeated
  full re-discovery. *Test:* role templates contain no round-robin language and reference the
  handoff outcome classes.

### Observability / recovery / compatibility (#13)
- **R9.** Status/logs expose **goal, run, dispatch, and attempt identity** and a recommended
  recovery action; a bare process exit or sentinel file is insufficient. *Evidence:* §5 fin
  v12 + missing `worker/0078`. *Test:* `crewd status` after a forced kill names the tainted
  attempt and the next action.
- **R10.** Distinguish `finished` / `aborted` / `paused` / `stopped` terminal states in an
  append-only-safe way (run/attempt identity), since terminal markers alone are ambiguous.
  *Evidence:* §3 sentinel over-fire, §5 fin v12. *Test:* an aborted epoch is not reported as
  completed.
- **R11.** Preserve existing operator surface (config load, goal epochs, role
  homes/worktrees, logs, inboxes, daemon controls, pause/resume, status) by default; any
  incompatibility ships with an explicit migration. *Evidence:* current README workflow.

---

## 9. Reliable next-role signals (for the Lead dispatcher)

From the evidence, the following signals predicted the genuinely useful next role far better
than "whose turn is it":

1. **Open PR awaiting review** → Verifier (only when a PR exists and its head changed since
   last review). legal's productive Verifier ticks cluster exactly here.
2. **Open `crewd:task` issue mentioning a role with no open PR for it** → that role (usually
   Worker). Absence of this signal is why fin Worker was 90%+ idle.
3. **Failed / cancelled / no-progress handoff** → Lead (R2), never auto-advance.
4. **Decision boundary / stated uncertainty / disagreement** → Advisory, on demand (§7).
5. **Human override in inbox or a blocking label** → Lead first (legal cy78).
6. **No open task, no open PR, no override** → idle/wait; invoke nothing (the case the fixed
   loop handled worst).

---

## 10. Confidence and open questions

- **High confidence:** the fixed-loop idle waste in fin steady state (full census + hand
  validation), the cumulative-footer measurement caveats, and the lifecycle/recovery gaps
  (complete exception census + the cy77 trace and code in `backends.py`).
- **Medium confidence:** exact semantic idle *rates* — the keyword classifier under-counts
  idle (§4) and per-tick latency is unrecoverable (§3), so idle fractions are lower bounds
  and delays are expressed in cycles.
- **Explicitly out of scope for this history:** it can establish *orchestration* requirements
  but **cannot** justify a specific SDK transport or session-lifecycle API. Choosing the SDK
  boundary in #10 needs a separate SDK capability/lifecycle experiment (create/resume/send,
  typed events, cancellation, disconnect), not these logs.

---

### Reproduction notes
- Population: all `state/logs/goal-v*/<role>/*.log` under the two read-only crew workspaces
  plus the aggregate `daemon.log` (6,574 role logs total).
- Stage-A parser: footer regex (`Changes +a -b`, `AI Credits`, `Resume`) + fixed keyword
  rubric. Stage-C: `random.seed(1729)`, 16-log hand read.
- Cited traces: legal `goal-v1/{worker,verifier,lead}/0077–0078`; fin
  `goal-v7/{worker/0325, lead/0081, verifier/0383}`, `goal-v12/*`.
