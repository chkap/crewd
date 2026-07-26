"""Dispatcher-driven run loop — replaces the fixed round-robin scheduler.

The legacy loop walked ``for role in ROLES`` every cycle. The orchestrator
instead drives the durable :class:`~crewd.dispatcher.Dispatcher` kernel: Lead
decides what runs next (via a journaled, budgeted *solicitation*), the decision
is consumed exactly once, and the chosen role attempt (or another Lead turn) is
executed through the typed :class:`~crewd.executor.AttemptExecutor` seam. Every
attempt is reserved before execution and terminalised after, so a crash never
replays work and restart reconciliation is deterministic.

One ``crewd run`` invocation advances the goal run until it leaves the ``active``
status — Lead finishes, waits, or pauses; the work budget exhausts; or an
operator control / signal halts it — mapping each terminal condition to a
distinct persisted exit reason. ``--once`` advances exactly one step.

Scope note (issue #17): explicit external-signal cancellation of an in-flight
attempt (``request_cancel`` + a distinct clean-cancel terminal) and
taint-before-finalize orphan-session recovery are a separate, independent
race handled in a follow-up slice. Here a signal halts the loop *between*
steps; the reserved/started attempt of an interrupted step is reconciled as
``uncertain`` on the next start (never replayed), and the orphaned SDK session
is left resumable rather than force-tainted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich.console import Console

from .config import CrewConfig, GoalState
from .dispatcher import (
    LEAD_PENDING,
    BudgetExhausted,
    DecisionKind,
    Dispatcher,
    DispatcherLimits,
    HandoffView,
    RunStatus,
)
from .executor import AttemptExecutor, AttemptRequest
from .workspace import Workspace

console = Console()


# Run status → durable exit reason for a single `crewd run` invocation.
_STATUS_EXIT = {
    RunStatus.FINISHED: "goal-complete",
    RunStatus.STOPPED: "goal-complete",
    RunStatus.PAUSED: "human-blocked",
    RunStatus.WAITING: "waiting",
    RunStatus.EXHAUSTED: "exhausted",
    RunStatus.INTERRUPTED: "interrupted",
}

_RESUMABLE = {
    RunStatus.PAUSED,
    RunStatus.WAITING,
    RunStatus.INTERRUPTED,
    RunStatus.STOPPED,
}


class Orchestrator:
    """Drives one goal run's dispatcher + executor to a terminal condition."""

    def __init__(
        self,
        ws: Workspace,
        cfg: CrewConfig,
        executor: AttemptExecutor,
        goal_state: GoalState,
        *,
        dispatcher: Dispatcher | None = None,
        max_steps: int = 10000,
    ):
        self.ws = ws
        self.cfg = cfg
        self.executor = executor
        self.goal_state = goal_state
        self.goal_label = goal_state.label or "goal:v1"
        self._owns_dispatcher = dispatcher is None
        self.disp = dispatcher or Dispatcher(
            self._db_path(ws),
            limits=DispatcherLimits(max_work=cfg.loop.max_cycles or 0),
        )
        self._max_steps = max_steps
        self._interrupted = False
        self._cycle = goal_state.cycles

    @staticmethod
    def _db_path(ws: Workspace) -> Path:
        ws.state_dir.mkdir(parents=True, exist_ok=True)
        return ws.state_dir / "dispatch.db"

    @property
    def configured_roles(self):
        return tuple(self.cfg.roles.keys())

    # ── signal handling (installed by the command layer) ──
    def request_stop(self, signum, frame):
        if self._interrupted:
            console.print("\n[red]double signal — exiting hard[/]")
            import sys

            sys.exit(130)
        console.print(
            f"\n[yellow]signal {signum} received — finishing current step then stopping[/]"
        )
        self._interrupted = True

    # ── main loop ──
    def run(self, once: bool) -> int:
        run = self.disp.start_or_resume_run(self.goal_label)
        # An explicit `crewd run` is intent to make progress: revive a resumable
        # (paused/waiting/interrupted/stopped) run, mirroring the workspace
        # sentinel clear the command layer already performed. Terminal
        # finished/exhausted runs are left as-is and reported below.
        if RunStatus(run.status) in _RESUMABLE:
            run = self.disp.resume_run(run.id)

        # Reconcile any in-flight attempt orphaned by a crash BEFORE new work.
        self.disp.reconcile_on_restart(run.id)

        steps = 0
        while True:
            halt = self._check_controls(run.id)
            if halt is not None:
                return self._exit(halt)

            run = self.disp.get_run(run.id)
            status = RunStatus(run.status)
            if status is not RunStatus.ACTIVE:
                return self._exit(_STATUS_EXIT.get(status, "goal-complete"))

            self._step(run.id, run.routing_authority)
            steps += 1

            if once:
                # Report a terminal reason if this single step reached one.
                run = self.disp.get_run(run.id)
                status = RunStatus(run.status)
                if status is not RunStatus.ACTIVE:
                    return self._exit(_STATUS_EXIT.get(status, "goal-complete"))
                return 0
            if steps >= self._max_steps:
                console.print(f"[blue]reached max_steps={self._max_steps}[/]")
                return self._exit("exhausted")

    # ── operator controls / signals (checked before every step) ──
    def _check_controls(self, run_id: str) -> Optional[str]:
        if self._interrupted:
            self._safe_mark(run_id, RunStatus.INTERRUPTED)
            console.print("[yellow]interrupted; exiting cleanly[/]")
            return "interrupted"
        if self.ws.is_stopped():
            self._safe_mark(run_id, RunStatus.STOPPED)
            console.print("[yellow]STOPPED sentinel present; exiting[/]")
            return "goal-complete"
        if self.ws.is_paused():
            self._safe_mark(run_id, RunStatus.PAUSED, self.ws.pause_reason())
            console.print(f"[yellow]PAUSED for human input:[/] {self.ws.pause_reason()}")
            return "human-blocked"
        return None

    def _safe_mark(self, run_id: str, status: RunStatus, blocker: str | None = None) -> None:
        from .dispatcher import DecisionError

        try:
            self.disp.mark_run_status(run_id, status, human_blocker=blocker)
        except DecisionError:
            # Run already terminal (finished/exhausted); the operator control is
            # moot — the terminal reason still governs the exit.
            pass

    # ── one orchestration step ──
    def _step(self, run_id: str, routing_authority: str) -> None:
        if routing_authority == LEAD_PENDING:
            self._lead_step(run_id)
        else:
            self._dispatch_step(run_id, routing_authority)

    def _lead_step(self, run_id: str) -> None:
        try:
            sol = self.disp.open_lead_solicitation(run_id)
        except BudgetExhausted:
            return  # run marked exhausted; the loop exits on the next iteration
        pending = self.disp.pending_handoffs(run_id)
        self._cycle += 1
        req = self._request("lead", self._lead_prompt(pending))
        console.print(f"  [magenta]lead[/] ({self.cfg.roles.get('lead', _NoModel()).model}) solicitation → {req.log_path}")
        turn = self.executor.run_lead(req)
        # Persist the Lead session identity before consuming the candidate.
        try:
            self.disp.mark_started(
                sol.attempt_id, session_id=turn.session_id, generation=turn.generation
            )
        except Exception:
            pass
        self.disp.resolve_lead_solicitation(
            sol.attempt_id,
            outcome=turn.result.outcome,
            decision=turn.decision,
            configured_roles=self.configured_roles,
        )
        self._persist_cycle()

    def _dispatch_step(self, run_id: str, dispatch_id: str) -> None:
        dsp = self.disp.get_dispatch(dispatch_id)
        role = dsp.role or "lead"
        if role not in self.cfg.roles:
            # A dispatch to an unconfigured role can only arise from a corrupt
            # journal; refuse to launch and let restart reconciliation handle it.
            console.print(f"[red]dispatch targets unconfigured role {role!r}; skipping[/]")
            return
        try:
            attempt_id = self.disp.reserve_attempt(run_id, dispatch_id, role)
        except BudgetExhausted:
            return
        self._cycle += 1
        req = self._request(role, self._role_prompt(role))
        console.print(f"  [magenta]{role}[/] ({self.cfg.roles[role].model}) → {req.log_path}")
        outcome = self.executor.execute_role(req)
        try:
            self.disp.mark_started(
                attempt_id, session_id=outcome.session_id, generation=outcome.generation
            )
        except Exception:
            pass
        result = outcome.result
        self.disp.record_terminal(
            attempt_id,
            result.outcome,
            evidence="",
            changed="",
            remaining=result.error or "",
            reason_returned=f"sdk:{result.outcome.value}",
        )
        self._persist_cycle()

    # ── request/prompt construction ──
    def _request(self, role: str, prompt: str) -> AttemptRequest:
        cfg_dir = self.ws.role_cfg_dir(role)
        wt = self.ws.role_worktree(role)
        add_dirs = [self.ws.root]
        if wt.exists():
            add_dirs.append(wt)
        add_dirs += self.ws.resolve_extra_dirs(self.cfg.extra_add_dirs)
        role_cfg = self.cfg.roles.get(role)
        model = role_cfg.model if role_cfg else "?"
        timeout = (role_cfg.per_tick_timeout if role_cfg and role_cfg.per_tick_timeout
                   else self.cfg.loop.per_tick_timeout)
        return AttemptRequest(
            role=role,
            model=model,
            prompt=prompt,
            config_dir=cfg_dir,
            add_dirs=add_dirs,
            cwd=cfg_dir,
            workspace_root=self.ws.root,
            goal_label=self.goal_label,
            timeout=float(timeout),
            log_path=self.ws.log_file(role, self._cycle, self.goal_label),
        )

    def _role_prompt(self, role: str) -> str:
        return (
            f"This is dispatch step {self._cycle}. Read the latest GitHub issues + "
            f"comments in `{self.cfg.target.remote}` and execute your role's "
            f"responsibilities. GOAL.md is at `{self.ws.goal_md}`. "
            f"If `{self.ws.state_dir / 'inbox' / (role + '.md')}` exists, read its "
            f"messages from the human operator FIRST, then truncate that file to "
            f"empty. Do one tick and stop."
        )

    def _lead_prompt(self, pending: list[HandoffView]) -> str:
        ids = [h.id for h in pending]
        if pending:
            lines = "\n".join(
                f"  - {h.id}: role={h.role} outcome={h.outcome_class.value} "
                f"reason={h.reason_returned}"
                for h in pending
            )
            pending_block = f"Pending handoffs awaiting your routing decision:\n{lines}\n"
        else:
            pending_block = "There are no pending handoffs (this is the run's first decision).\n"
        return (
            f"This is Lead decision step {self._cycle} for `{self.cfg.target.remote}`. "
            f"GOAL.md is at `{self.ws.goal_md}`.\n"
            f"{pending_block}"
            f"Decide what happens next and submit exactly one decision via the "
            f"`submit_lead_decision` tool. Your decision MUST acknowledge exactly "
            f"these handoff ids: {ids}. Valid kinds: dispatch (with a configured "
            f"role: {list(self.configured_roles)}), continue_lead, wait (with "
            f"wake_condition), pause (with human_blocker), finish (with "
            f"final_acceptance)."
        )

    # ── bookkeeping ──
    def _persist_cycle(self) -> None:
        self.goal_state.cycles = self._cycle
        try:
            self.goal_state.save(self.ws.goal_json)
            self.ws.write_cycle(self._cycle)
        except Exception:
            pass

    def _exit(self, reason: str) -> int:
        self.ws.state_dir.mkdir(parents=True, exist_ok=True)
        self.ws.exit_reason_file.write_text(reason + "\n")
        console.print(f"[blue]exit-reason:[/] {reason}")
        if self._owns_dispatcher:
            self.disp.close()
        return 0


class _NoModel:
    model = "?"
