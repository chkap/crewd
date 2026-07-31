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

Structured role handoffs (issue #12): a dispatched non-Lead role returns its
outcome to Lead through the ``submit_role_handoff`` SDK tool (parallel to Lead's
``submit_lead_decision``). The transport lifecycle stays authoritative — an
error, wait-timeout, cancel, or taint overrides any success-shaped claim, and
only a clean idle turn lets the role claim ``completed`` vs ``no_progress``; a
zero/multiple/malformed submission becomes an ``uncertain`` protocol failure
(counting toward the no-progress bounds), never a silent completion. The
resolved evidence/changed/remaining then flow verbatim into Lead's next
solicitation, so Lead routes on the role's own structured context rather than a
bare SDK code. See :func:`crewd.executor.resolve_role_terminal`.

Cancellation & recovery (issue #17): an interrupt or operator STOP cancels the
in-flight attempt promptly — the attempt runs on a worker thread while the main
loop polls controls and *requests* a non-blocking SDK abort through a single
:class:`~crewd.session_backend.CancelToken`. Timeout, signal, and operator stop
therefore share one cancellation owner (the attempt state machine), with no
double escalation; an externally cancelled turn that settles idle is a distinct
``cancelled_clean`` outcome, never a completion, and an unconfirmed abort taints.
On restart, each attempt still ``started`` (its session identity already
journaled **before** any SDK send, via the executor's pre-send ``on_started``
hook) has its orphaned session generation tainted **before** the uncertain
handoff is finalized, so a crashed generation is never resumed normally — and
because the taint is idempotent and the finalize is atomic, recovery is itself
durable and retryable.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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
from .executor import AttemptExecutor, AttemptRequest, resolve_role_terminal
from .github_bus import Route
from .inbox import InboxService
from .public_writer import is_material_handoff
from .session_backend import CancelToken
from .workspace import Workspace

console = Console()


# Run status → durable exit reason for a single `crewd run` invocation.
# Operator STOP is deliberately distinct from goal completion: a stop halts the
# run without any final acceptance, so it must never masquerade as `goal-complete`.
_STATUS_EXIT = {
    RunStatus.FINISHED: "goal-complete",
    RunStatus.STOPPED: "stopped",
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


@dataclass(frozen=True)
class GateBlock:
    """A refused authority transition, classified for recovery (issue #49).

    ``route`` separates a *recoverable* condition that self-heals — ``Route.WAIT``:
    a transient GitHub outage, a closed-target ordering/closure race, a stale
    read-after-write, an ambiguous-but-landed write, or a deferred-yet-
    reconcilable terminal publish — from a genuine *human* blocker —
    ``Route.PAUSE``: a real credential / permission / policy denial. A
    ``Route.REJECT`` is an invalid *public record* (wrong repo/goal, malformed
    attribution) that will not fix itself on retry.

    When a ``Route.REJECT`` is a Lead-*correctable* inconsistency (#64) the block
    carries a typed ``correction`` (a serialized :class:`~crewd.github_bus.
    GateCorrection`): the orchestrator returns it to Lead — keeping the same task
    binding and pending handoffs, reserving no attempt — instead of pausing, so a
    correctable public-record gap is never mislabeled as a human blocker. Only a
    genuine operator prerequisite (permission, wrong-repo config) or the bounded
    escalation still pauses.

    The orchestrator marks the run ``WAITING`` for a recoverable block — a later
    ``crewd run`` reconciles the durable intent and continues *without* operator
    action — and ``PAUSED`` only for a real human blocker. This is the fix for a
    terminal public-write ordering race being mis-reported as ``human-blocked``.
    """

    route: Route
    detail: str
    correction: Optional[str] = None


# A gate block's route → the durable run status it drives. Recoverable routes
# self-heal on the next run's reconcile; only a human blocker pauses.
_BLOCK_STATUS = {
    Route.WAIT: RunStatus.WAITING,
    Route.PAUSE: RunStatus.PAUSED,
    Route.REJECT: RunStatus.PAUSED,
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
        poll_interval: float = 0.05,
        taint_orphan: Optional[Callable[[str, int, str], None]] = None,
        prompt_policy: object | None = None,
        inbox: InboxService | None = None,
        bus_gate: object | None = None,
        publisher: object | None = None,
    ):
        self.ws = ws
        self.cfg = cfg
        self.executor = executor
        self.goal_state = goal_state
        # Host-owned operator inbox delivery (issue #29): the orchestrator, not
        # model best effort, reads/attaches/archives operator messages so an
        # OVERRIDE cannot be silently skipped. Injectable for deterministic tests.
        self._inbox = inbox or InboxService.for_workspace(ws)
        # Public-bus transaction boundary (issue #29): an optional gate that must
        # verify GitHub prerequisites before a Worker/Verifier attempt is
        # reserved. Default None keeps production inert until a client is wired,
        # so authority routing is unchanged when no boundary is configured.
        self._bus_gate = bus_gate
        # Durable attributed public-write publisher (issue #29): publishes every
        # material role handoff / Lead decision as a verified GitHub artifact
        # exactly once, with a durable intent journal + restart reconciliation.
        # Default None keeps production inert until a client is wired, so authority
        # routing is unchanged when no publisher is configured. When present the
        # orchestrator (a) publishes material role handoffs at their terminal, (b)
        # reconciles pending intents on run start, and (c) refuses to consume a
        # material handoff whose public artifact is not yet verified.
        self._publisher = publisher
        # Gated, test-only seam (default None → fully inert in production). When a
        # live-smoke policy is injected it may append a bounded instruction suffix
        # to the *production-rendered* role/Lead prompts and observe (not replace)
        # them. It never alters the production handoff rendering, so the payload a
        # real Lead turn receives is exactly the production one. See crewd._smoke.
        self._prompt_policy = prompt_policy
        self.goal_label = goal_state.label or "goal:v1"
        self._owns_dispatcher = dispatcher is None
        self.disp = dispatcher or Dispatcher(
            self._db_path(ws),
            limits=DispatcherLimits(max_work=cfg.loop.max_cycles or 0),
        )
        self._max_steps = max_steps
        self._interrupted = False
        self._cycle = goal_state.cycles
        # How often the control poll checks for an interrupt / operator stop while
        # an attempt is in flight (so cancellation is prompt but cheap).
        self._poll_interval = poll_interval
        # Injectable so deterministic tests can observe the orphan-session taint
        # without the real per-role taint file; defaults to the durable taint.
        self._taint_orphan = taint_orphan or self._taint_orphan_session

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
    def run(self, once: bool, resume: bool = False) -> int:
        run = self.disp.start_or_resume_run(self.goal_label)
        status = RunStatus(run.status)
        # A recoverable public-write WAIT is system-owned: reconcile it before the
        # non-active early exit, then reactivate the run once every pending write
        # has either verified or reached a typed terminal disposition. This keeps
        # ordinary closure/API races self-healing under plain ``crewd run`` while
        # preserving explicit resume for Lead-authored waits and human pauses.
        if (
            status is RunStatus.WAITING
            and self._publisher is not None
            and self._publisher.has_recoverable_pending()
        ):
            self._reconcile_public_writes(force=True)
            if not self._publisher.has_recoverable_pending():
                run = self.disp.resume_run(run.id)
                status = RunStatus(run.status)
        # A non-active run holds a durable state — a paused human blocker, a wait
        # condition, an interrupt, an operator stop, or a terminal
        # finished/exhausted. A plain `crewd run` MUST NOT erase it: reviving a
        # paused/waiting/interrupted/stopped run is the *explicit* resume
        # workflow's job only (mirroring the dispatcher's own contract that
        # start_or_resume_run never auto-revives). Terminal finished/exhausted are
        # never resumable. This closes the durable-pause bypass (PR #16): a plain
        # run can no longer silently clear a Lead human blocker.
        if status is not RunStatus.ACTIVE:
            if resume and status in _RESUMABLE:
                run = self.disp.resume_run(run.id)
            else:
                return self._exit(_STATUS_EXIT.get(status, "goal-complete"))

        # Reconcile any in-flight attempt orphaned by a crash BEFORE new work.
        # Orphaned `started` sessions are tainted before their uncertain handoff
        # is finalized, so a crashed in-flight generation is never resumed
        # normally (recovery advances to a fresh generation).
        self.disp.reconcile_on_restart(run.id, taint_orphan=self._taint_orphan)

        # Reconcile durable public-write intents left reserved by a crash between
        # "post intended" and "post verified" (issue #29, GOAL #5). Idempotent via
        # the correlation marker, so a landed write is not double-posted. Best
        # effort: a still-unavailable GitHub leaves the intent reserved and surfaces
        # in status/doctor; it does not abort the run.
        self._reconcile_public_writes()

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
            return "stopped"
        if self.ws.is_paused():
            self._safe_mark(run_id, RunStatus.PAUSED, self.ws.pause_reason())
            console.print(f"[yellow]PAUSED for human input:[/] {self.ws.pause_reason()}")
            return "human-blocked"
        return None

    def _safe_mark(
        self, run_id: str, status: RunStatus,
        blocker: str | None = None, *, wake_condition: str | None = None,
    ) -> None:
        from .dispatcher import DecisionError

        try:
            self.disp.mark_run_status(
                run_id, status, human_blocker=blocker, wake_condition=wake_condition
            )
        except DecisionError:
            # Run already terminal (finished/exhausted); the operator control is
            # moot — the terminal reason still governs the exit.
            pass

    def _apply_gate_block(self, run_id: str, block: "GateBlock", context: str) -> None:
        """Halt the run for a refused transition, self-healing where possible.

        A recoverable block (``Route.WAIT``: transient outage, closed-target
        closure race, stale read-after-write, deferred-but-reconcilable terminal
        publish) marks the run ``WAITING`` — a later ``crewd run`` reconciles the
        durable public-write intent and continues *without* any operator action.
        Only a genuine human blocker (``Route.PAUSE``: credential/permission/
        policy) or an invalid public record (``Route.REJECT``) marks the run
        ``PAUSED``/``human-blocked`` (issue #49).
        """
        status = _BLOCK_STATUS.get(block.route, RunStatus.PAUSED)
        if status is RunStatus.WAITING:
            self._safe_mark(run_id, status, wake_condition=block.detail)
            console.print(
                f"[yellow]{context}: {block.detail}; run waiting — a later "
                f"`crewd run` reconciles the public write and continues[/]"
            )
        else:
            self._safe_mark(run_id, status, block.detail)
            console.print(f"[yellow]{context}: {block.detail}; run paused[/]")

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
        prompt = self._deliver_inbox("lead", sol.attempt_id, self._lead_prompt(pending, run_id))
        req = self._request("lead", prompt)
        console.print(f"  [magenta]lead[/] ({self.cfg.roles.get('lead', _NoModel()).model}) solicitation → {req.log_path}")
        # Journal the Lead session identity durably BEFORE any SDK send, via the
        # executor's on_started hook. Persistence failure is NOT swallowed: it
        # propagates and aborts the run rather than continuing with an unjournaled
        # in-flight attempt (restart reconciliation then handles the solicitation).
        turn = self._execute_cancellably(
            lambda cancel: self.executor.run_lead(
                req, on_started=self._on_started(sol.attempt_id), cancel=cancel
            )
        )
        # Public-bus pre-application gate (issue #29): a Lead decision that would
        # advance authority (dispatch to Worker/Verifier, or finish) may not be
        # applied until the required GitHub artifacts are verified. Validation
        # happens BEFORE `resolve_lead_solicitation` applies the decision, so a
        # blocked transition never acks the decision's pending handoffs or
        # transfers authority (GOAL.md: invalid references fail without consuming
        # handoffs or transferring authority). When blocked, the candidate
        # decision is dropped (passed as None) — the dispatcher then returns
        # authority to Lead with the same pending handoffs intact — and the run is
        # paused with the descriptive blocker below.
        decision = turn.decision
        gate_blocker = self._decision_gate_block(decision, pending)
        # Pre-application publication gate for a material Lead *routing* decision
        # (dispatch): the decision's public artifact must be reserved, posted, and
        # verified BEFORE `resolve_lead_solicitation` applies it, so authority
        # never advances to the dispatched role while the routing decision has no
        # verified public record (GOAL.md: material Lead routing is public before
        # internal progress; an unverified write is never proof). On WAIT/PAUSE/
        # REJECT the candidate decision is dropped, leaving authority with Lead and
        # the pending handoffs intact; the durable reserved intent is reconciled on
        # a later run (idempotently — the correlation id is stable for this
        # authority epoch, so a retry never double-posts).
        if gate_blocker is None:
            gate_blocker = self._lead_decision_publish_block(decision, run_id, pending)
        # A Lead-correctable gate rejection (#64) is routed back to Lead as a
        # typed correction rather than pausing: the same pending handoffs stay
        # intact, no attempt is reserved, and the dispatcher records the
        # correction (bounded — after the correction streak cap the run settles
        # into a recoverable WAITING state, never a human PAUSE). Every other
        # block keeps its existing recovery disposition (WAIT self-heals;
        # PAUSE/permission halts).
        #
        # For a non-correctable block the candidate decision is dropped here so it
        # can never be applied. For a correctable block the REAL candidate is
        # passed through so the dispatcher can prove the solicitation clean and
        # valid for the current authority window BEFORE recording any correction:
        # a failed/malformed/stale solicitation combined with a correctable public
        # rejection takes the invalid/stale path in the dispatcher and is never
        # downgraded to a benign correction.
        correctable = gate_blocker is not None and gate_blocker.correction is not None
        if gate_blocker is not None and not correctable:
            decision = None
        result = self.disp.resolve_lead_solicitation(
            sol.attempt_id,
            outcome=turn.result.outcome,
            decision=decision,
            configured_roles=self.configured_roles,
            gate_correction=(gate_blocker.correction if correctable else None),
        )
        # Only archive the delivered operator inbox once the solicitation reached a
        # productive terminal step. An invalid Lead decision OR a correctable gate
        # rejection returns authority to Lead for another turn, so the message must
        # NOT be archived here — leaving the attempt's staging file in place lets
        # the next Lead solicitation's deliver() re-absorb it, retaining the
        # OVERRIDE across the retry (GOAL.md inbox retry invariant, issue #29).
        if not result.solicitation_invalid and not result.gate_corrected:
            self._inbox.acknowledge("lead", sol.attempt_id)
        if self._prompt_policy is not None:
            self._prompt_policy.record_lead_decision(turn.decision)
        if gate_blocker is not None and not result.gate_corrected:
            # The transition was refused and NOT recorded as a correction (either a
            # non-correctable block, or a correctable rejection whose solicitation
            # was itself invalid/stale and so took the invalid path). Authority
            # stayed with Lead (decision was not applied, so no handoff was acked
            # and the run was not terminalised/dispatched). For a real gate block,
            # halt with a route-classified public-bus blocker: a recoverable
            # ordering/outage condition WAITS and self-heals on the next run's
            # reconcile; only a real human blocker PAUSES (#49). For an invalid
            # solicitation the dispatcher already returned authority to Lead, so
            # only apply the gate block when the transition was actually refused by
            # the gate (not merely an invalid Lead turn).
            if not correctable:
                self._apply_gate_block(
                    run_id, gate_blocker, "public-bus gate blocked Lead decision"
                )
        elif result.gate_corrected:
            # Authority stayed with Lead; the correction is durably recorded and
            # surfaced in the next Lead solicitation. If the dispatcher reached the
            # correction-streak cap the run is already WAITING (bounded, recoverable
            # — not a human PAUSE).
            console.print(
                f"[yellow]public-bus returned a correctable gate rejection to Lead: "
                f"{gate_blocker.detail}[/]"
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
        # Public-bus prerequisite gate (issue #29): before reserving the attempt,
        # verify the GitHub record satisfies the invariant for routing this role.
        # A non-PROCEED outcome halts the run (durable PAUSE with a descriptive
        # public-bus blocker) WITHOUT reserving the attempt or consuming a pending
        # handoff — authority never advances on an invalid/unverifiable record.
        if not self._bus_gate_ok(run_id, role, dsp):
            return
        try:
            attempt_id = self.disp.reserve_attempt(run_id, dispatch_id, role)
        except BudgetExhausted:
            return
        self._cycle += 1
        prompt = self._deliver_inbox(role, attempt_id, self._role_prompt(role, dsp))
        req = self._request(role, prompt)
        console.print(f"  [magenta]{role}[/] ({self.cfg.roles[role].model}) → {req.log_path}")
        # Journal session identity BEFORE the SDK send (see _lead_step). This is
        # what makes the deferred taint/orphan-recovery follow-up safe: an attempt
        # that reaches the transport is always durably `started` with its session.
        outcome = self._execute_cancellably(
            lambda cancel: self.executor.execute_role(
                req, on_started=self._on_started(attempt_id), cancel=cancel
            )
        )
        result = outcome.result
        # Resolve the routing terminal transport-authoritatively: the SDK
        # lifecycle outcome governs unless the turn reached a clean idle, in which
        # case the role's exactly-one structured `submit_role_handoff` claim
        # (completed vs no_progress) governs; a zero/multiple/malformed submission
        # is a protocol failure recorded as `uncertain`, never a silent
        # completion. Evidence/changed/remaining flow through to Lead's next
        # solicitation so it routes on the richest available information.
        terminal = resolve_role_terminal(
            result, outcome.handoff, outcome.handoff_submissions
        )
        handoff_id = self.disp.record_terminal(
            attempt_id,
            result.outcome,
            outcome_class=terminal.outcome_class,
            evidence=terminal.evidence,
            changed=terminal.changed,
            remaining=terminal.remaining,
            reason_returned=terminal.reason_returned,
            disagreement=terminal.disagreement,
            blocker=terminal.blocker,
        )
        # Publish the role's material handoff as a verified GitHub artifact keyed
        # by the durable handoff id (issue #29, GOAL #2). Best-effort here: a
        # publish failure leaves a durable reserved intent (reconciled on restart)
        # and does NOT terminalise-block the attempt. The consume-time enforcement
        # in `_decision_gate_block` is what guarantees Lead cannot ack a material
        # handoff whose artifact is not yet verified.
        self._publish_role_handoff(handoff_id, role, terminal)
        # Attempt is durably terminal → archive the delivered operator inbox.
        self._inbox.acknowledge(role, attempt_id)
        self._persist_cycle()

    def _lead_decision_publish_block(self, decision: object, run_id: str, pending: list) -> Optional["GateBlock"]:
        """Reserve/post/verify a material Lead *dispatch* decision artifact BEFORE
        it is applied; return a :class:`GateBlock` to drop the decision, or
        ``None`` to allow.

        Only a dispatch (Lead → a role) is a material inter-role routing decision
        that must be public before authority advances; continue-lead/wait/pause are
        Lead-internal and finish is governed by the finish-prerequisite record.

        The correlation id is stable for *this logical decision* across pause/resume
        retries: it is derived from the run, the exact set of pending handoff ids
        the decision consumes, and the target role. A blocked decision consumes
        nothing (the pending set is unchanged on the retried solicitation), so the
        reserved intent reconciles under the *same* marker and the re-decided
        publish deduplicates to it — never a double-post. (``authority_seq`` is
        deliberately NOT used: a pause bumps it, so it is not stable across a
        retry.) Once the decision is applied the pending handoffs are consumed, so
        the next decision derives a distinct id.

        Returns ``None`` when no publisher is wired (production-inert) or the
        decision is not a dispatch. A publish that does not verify carries the
        publish route so a transient outage self-heals (WAIT) rather than pausing.
        """
        publisher = self._publisher
        if publisher is None or decision is None:
            return None
        if getattr(decision, "kind", None) is not DecisionKind.DISPATCH:
            return None
        target_role = getattr(decision, "role", "") or ""
        pending_ids = "+".join(sorted(h.id for h in (pending or [])))
        correlation_id = f"lead-dispatch:{run_id}:{pending_ids}:{target_role}"
        try:
            outcome = publisher.publish_lead_decision(
                decision_id=correlation_id, kind="dispatch",
                target_role=target_role,
                reason=getattr(decision, "reason", "") or "",
                task_number=getattr(decision, "task_number", None),
            )
        except Exception as exc:  # a publish bug must not fake a verified write
            return GateBlock(Route.PAUSE, f"public-bus lead decision publish error: {exc}")
        if outcome is not None and outcome.verified:
            return None
        detail = outcome.detail if outcome is not None else "not published"
        route = outcome.route if outcome is not None else Route.WAIT
        return GateBlock(
            route,
            f"public-bus lead dispatch decision unverified "
            f"(-> {target_role or 'role'}): {detail}",
        )

    def _reconcile_public_writes(self, *, force: bool = False) -> None:
        """Finish any durable public-write intents left reserved by a crash."""
        publisher = self._publisher
        if publisher is None:
            return
        try:
            results = publisher.reconcile(force=force)
        except Exception as exc:  # reconciliation must never abort the run
            console.print(f"[yellow]public-write reconcile error: {exc}[/]")
            return
        pending = [r for r in results if not r.verified]
        if pending:
            console.print(
                f"[yellow]{len(pending)} public write(s) still unverified after "
                "reconcile; see status/doctor[/]"
            )

    def _publish_role_handoff(self, handoff_id: str, role: str, terminal: object):
        """Publish a role's material handoff as a verified GitHub artifact.

        ``terminal`` is any object exposing the handoff fields (a
        :class:`~crewd.executor.RoleTerminal` at record time or a
        :class:`~crewd.dispatcher.HandoffView` at consume time). Returns the
        :class:`~crewd.public_writer.PublishOutcome`, or ``None`` when no publisher
        is wired or the handoff is a private ``no_progress`` (not material).

        The task the artifact is posted to is the *routed* task bound to the
        handoff's dispatch (issue #47), resolved from the durable journal — never
        a later global re-census — so a second queued task cannot divert it.
        """
        publisher = self._publisher
        if publisher is None:
            return None
        oc = getattr(terminal, "outcome_class", "")
        oc = getattr(oc, "value", oc)
        reason = getattr(terminal, "reason_returned", "") or ""
        if not is_material_handoff(
            oc,
            evidence=terminal.evidence, changed=terminal.changed,
            remaining=terminal.remaining, disagreement=terminal.disagreement,
            blocker=terminal.blocker,
        ):
            return None
        task_number = pr_number = None
        try:
            task_number, pr_number = self.disp.binding_for_handoff(handoff_id)
        except Exception:  # journal read must not fake a verified write
            task_number = pr_number = None
        try:
            return publisher.publish_role_handoff(
                handoff_id=handoff_id, role=role, outcome_class=oc,
                task_number=task_number, pr_number=pr_number,
                evidence=terminal.evidence, changed=terminal.changed,
                remaining=terminal.remaining, reason=reason,
                disagreement=terminal.disagreement, blocker=terminal.blocker,
            )
        except Exception as exc:  # a publish bug must not fake a verified write
            console.print(f"[yellow]public-write publish error ({role}): {exc}[/]")
            return None

    def _bus_gate_ok(self, run_id: str, role: str, dsp: object) -> bool:
        """Consult the public-bus gate (if any) before reserving an attempt.

        Defense-in-depth for a dispatch resurrected from the journal after a
        restart (the normal path is already gated before the dispatch is created;
        see :meth:`_decision_gate_block`). The persisted ``task_number`` on the
        dispatch preserves the routed-task binding across the restart (issue #47),
        so the gate re-validates the same task rather than re-censusing the
        record. Returns True to proceed. On a non-``PROCEED`` outcome it halts the
        run via :meth:`_apply_gate_block` (WAITING for a recoverable condition,
        PAUSED for a human blocker) and returns False, so no attempt is reserved.
        """
        gate = self._bus_gate
        if gate is None:
            return True
        intent = getattr(dsp, "intent", None)
        task_number = getattr(dsp, "task_number", None)
        try:
            outcome = gate.evaluate(
                role, dsp, task_number=task_number, intent=intent
            )
        except Exception as exc:
            outcome = None
            block = GateBlock(Route.WAIT, f"public-bus gate error: {exc}")
        else:
            block = self._classify_gate_block(role, intent, task_number, outcome)
        if block is not None:
            self._apply_gate_block(
                run_id, block, f"public-bus gate blocked {role} dispatch"
            )
            return False
        # An implementation-review Verifier dispatch must resolve (and durably
        # bind) the exact linked PR. A Lead-assigned verifier-only intent
        # (audit/acceptance/release/advisory) has no Worker PR to review, so the
        # PR-binding requirement does not apply (#64/#61).
        if role == "verifier" and self._is_implementation_intent(intent):
            pr_number = (outcome.refs or {}).get("pr") if outcome is not None else None
            if not pr_number:
                self._apply_gate_block(
                    run_id,
                    GateBlock(Route.WAIT, "Verifier gate returned no exact linked PR"),
                    "public-bus gate blocked verifier dispatch",
                )
                return False
            try:
                self.disp.bind_pr_to_dispatch(dsp.id, int(pr_number))
            except Exception as exc:
                self._apply_gate_block(
                    run_id,
                    GateBlock(Route.WAIT, f"cannot persist Verifier PR binding: {exc}"),
                    "public-bus gate blocked verifier dispatch",
                )
                return False
        return True

    @staticmethod
    def _is_implementation_intent(intent) -> bool:
        """Whether a dispatch intent is a normal implementation review.

        Missing/legacy/unknown → True (the conservative default that keeps the
        linked-PR + Worker-readiness safeguards)."""
        if intent is None:
            return True
        val = str(getattr(intent, "value", intent)).strip().lower()
        return val not in ("verifier_audit", "acceptance", "release", "advisory")

    def _decision_gate_block(self, decision: object, pending: list | None = None) -> Optional["GateBlock"]:
        """Return a public-bus :class:`GateBlock` for a Lead decision that would
        advance authority, or ``None`` to allow it.

        Called BEFORE the decision is applied, so a blocked dispatch/finish never
        acks the decision's pending handoffs or transfers authority. A dispatch to
        Worker/Verifier is validated against the record; a ``finish`` is validated
        against the final-acceptance record. Independently, *any* decision that
        would acknowledge a material role handoff (GOAL #2) is refused until that
        handoff's public artifact is verified — so an internal handoff is never
        consumed while its public record is missing (a crashed/unavailable public
        write must not become a silently-completed handoff). All other checks
        (continue-lead, wait, pause, dispatch to other roles) are unrelated to the
        public-bus prerequisite and always allowed.
        """
        if decision is None:
            return None
        # Consume-time enforcement: a material handoff may not be acked until its
        # public artifact is verified. Runs for every decision kind that acks
        # handoffs (dispatch, continue-lead, wait, pause, finish).
        material_blocker = self._material_handoff_block(decision, pending)
        if material_blocker is not None:
            return material_blocker
        kind = getattr(decision, "kind", None)
        if kind is DecisionKind.DISPATCH:
            return self._dispatch_gate_block(
                getattr(decision, "role", None) or "",
                getattr(decision, "task_number", None),
                getattr(decision, "intent", None),
            )
        if kind is DecisionKind.FINISH:
            return self._finish_gate_block()
        return None

    def _material_handoff_block(self, decision: object, pending: list | None) -> Optional["GateBlock"]:
        """Refuse a decision that would ack a material handoff whose public
        artifact is not yet verified.

        Only active when a publisher is wired (production-inert otherwise). For
        each acknowledged handoff that :func:`is_material_handoff` classifies as
        material, the durable intent journal must show a verified artifact; if a
        publish was deferred by a prior GitHub outage this attempts to finish it
        first (reconcile), and only blocks if it still cannot verify. A block drops
        the decision, so the handoff stays pending (no consumption) until the
        artifact lands.

        The returned :class:`GateBlock` carries the publish route so a terminal
        write still racing a merge/close, a transient outage, or a deferred-but-
        reconcilable publish self-heals (``Route.WAIT`` → the run WAITS and the
        next run reconciles) instead of being mis-reported as ``human-blocked``
        (issue #49). Only a genuine ``Route.PAUSE`` (permission) pauses.
        """
        publisher = self._publisher
        if publisher is None:
            return None
        ack_ids = set(getattr(decision, "ack_handoff_ids", ()) or ())
        if not ack_ids:
            return None
        by_id = {h.id: h for h in (pending or [])}
        for hid in ack_ids:
            handoff = by_id.get(hid)
            if handoff is None:
                continue
            if not is_material_handoff(
                getattr(handoff.outcome_class, "value", handoff.outcome_class),
                evidence=handoff.evidence, changed=handoff.changed,
                remaining=handoff.remaining, disagreement=handoff.disagreement,
                blocker=handoff.blocker,
            ):
                continue
            if publisher.is_verified(hid):
                continue
            # Deferred/failed earlier — try to finish the publish now.
            outcome = self._publish_role_handoff(hid, handoff.role, handoff)
            if outcome is not None and outcome.verified:
                continue
            detail = outcome.detail if outcome is not None else "not published"
            # No outcome (no task binding yet / publish bug) is a deferred write
            # the next reconcile can finish — recoverable, so WAIT not PAUSE.
            route = outcome.route if outcome is not None else Route.WAIT
            return GateBlock(
                route,
                f"public-bus write unverified for {handoff.role} handoff {hid}: {detail}",
            )
        return None

    def _classify_gate_block(
        self, role: str, intent, task_number, outcome
    ) -> Optional["GateBlock"]:
        """Turn a non-PROCEED gate outcome into a :class:`GateBlock` (#64).

        A Lead-correctable ``Route.REJECT`` is enriched with a typed
        :class:`~crewd.github_bus.GateCorrection` and returned to Lead instead of
        pausing; every other route keeps its existing recovery disposition.
        """
        if outcome is None or outcome.route is Route.PROCEED:
            return None
        gate = self._bus_gate
        reason = getattr(outcome, "reason", None)
        if (
            outcome.route is Route.REJECT
            and reason is not None
            and getattr(reason, "lead_correctable", False)
            and gate is not None
            and hasattr(gate, "build_correction")
        ):
            correction = gate.build_correction(
                role, intent, outcome, task_number=task_number
            )
            return GateBlock(
                Route.REJECT,
                f"public-bus correction: {correction.summary()}",
                correction=correction.to_json(),
            )
        return GateBlock(outcome.route, f"public-bus {outcome.route.value}: {outcome.detail}")

    def _dispatch_gate_block(
        self, role: str, task_number: Optional[int] = None, intent=None,
    ) -> Optional["GateBlock"]:
        """Consult the public-bus gate for a Worker/Verifier dispatch (if any).

        ``task_number`` is the exact routed task carried by the Lead decision (at
        decision time) or persisted with the dispatch (at reserve time after a
        restart). It is validated against that bound task — never a later global
        re-census (issue #47) — so a stale/mismatched binding fails *before* role
        execution with a recoverable blocker and a second queued task cannot
        divert the gate. A gate that raises does not silently pass: the error
        becomes a human blocker (a boundary bug is not self-healing).
        """
        gate = self._bus_gate
        if gate is None:
            return None
        try:
            outcome = gate.evaluate(role, None, task_number=task_number, intent=intent)
        except Exception as exc:  # boundary bug must not fake success
            return GateBlock(Route.PAUSE, f"public-bus gate error: {exc}")
        return self._classify_gate_block(role, intent, task_number, outcome)

    def _finish_gate_block(self) -> Optional["GateBlock"]:
        """Consult the public-bus gate for a Lead ``finish`` (if any).

        Returns ``None`` to allow the finish, or a :class:`GateBlock` when the
        final-acceptance record is missing/invalid/unverifiable. A gate that lacks
        a finish check, or that has no gate at all, allows finish. A gate that
        raises does not silently pass: the error becomes a human blocker.
        """
        gate = self._bus_gate
        if gate is None:
            return None
        evaluate_finish = getattr(gate, "evaluate_finish", None)
        if evaluate_finish is None:
            return None
        try:
            outcome = evaluate_finish()
        except Exception as exc:  # boundary bug must not fake success
            return GateBlock(Route.PAUSE, f"public-bus finish gate error: {exc}")
        if outcome is None or outcome.route is Route.PROCEED:
            return None
        return GateBlock(outcome.route, f"public-bus {outcome.route.value}: {outcome.detail}")

    def _on_started(self, attempt_id: str):
        """Return the pre-send journaling callback for an in-flight attempt.

        Invoked by the executor after session selection and before any SDK send.
        A raised :class:`~crewd.dispatcher.DecisionError` propagates (not
        swallowed) so persistence failure surfaces instead of silently launching
        an unjournaled attempt.
        """

        def _cb(session_id: str, generation: int) -> None:
            self.disp.mark_started(attempt_id, session_id=session_id, generation=generation)

        return _cb

    # ── in-flight cancellation ──────────────────────────────────
    def _execute_cancellably(self, fn: "Callable[[CancelToken], object]"):
        """Run one attempt on a worker thread while polling operator controls.

        The attempt (``fn``) runs off the main thread so the main thread stays
        free to observe an interrupt/operator-stop and *request* cancellation via
        a single :class:`~crewd.session_backend.CancelToken`. This poll loop is the
        sole cancellation *requester* (timeout, signal, and operator stop all
        funnel through the one token, so there is no double escalation); the
        attempt state machine remains the sole cancellation *owner*. The request
        is non-blocking: it pokes the SDK to abort the in-flight turn and lets the
        state machine confirm/escalate. Exceptions raised by ``fn`` (e.g. a
        pre-send journaling failure) propagate unchanged.
        """
        cancel = CancelToken()
        box: dict = {}

        def _worker() -> None:
            try:
                box["result"] = fn(cancel)
            except BaseException as e:  # re-raised on the main thread below
                box["error"] = e

        t = threading.Thread(target=_worker, name="crewd-attempt", daemon=True)
        t.start()
        while t.is_alive():
            if not cancel.is_requested:
                reason = self._cancel_reason()
                if reason is not None:
                    console.print(
                        f"[yellow]cancelling in-flight attempt (non-blocking abort): {reason}[/]"
                    )
                    cancel.request(reason)
            t.join(timeout=self._poll_interval)
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _cancel_reason(self) -> Optional[str]:
        """The reason to cancel an in-flight attempt now, or ``None``.

        A pending interrupt or operator STOP both demand the active attempt be
        cancelled promptly rather than after it finishes; the between-steps
        controls (:meth:`_check_controls`) then map the run to its exit reason.
        """
        if self._interrupted:
            return "signal"
        if self.ws.is_stopped():
            return "operator-stop"
        return None

    def _taint_orphan_session(self, session_id: str, generation: int, role: str) -> None:
        """Durably taint an orphaned session generation found on restart.

        Uses the same per-role taint file the SDK executor consults, so the next
        session decision for this role refuses to resume the orphaned id and
        advances to a fresh recovery generation. Idempotent (safe to retry if
        recovery itself crashed mid-way).
        """
        from .session_backend import TaintStore

        store = TaintStore(self.ws.role_cfg_dir(role) / ".crewd-sdk-taint")
        store.taint(session_id)
        console.print(
            f"[yellow]restart: orphaned session tainted (role={role} gen={generation}); "
            f"recovery will start a fresh generation[/]"
        )

    # ── request/prompt construction ──
    def _deliver_inbox(self, role: str, attempt_id: str, base_prompt: str) -> str:
        """Prepend host-delivered operator inbox messages (if any) to a prompt.

        The host stages the role's live inbox against this attempt so the payload
        is attached by the runtime — not fetched by the model — and archived only
        after the attempt terminalizes (see :class:`crewd.inbox.InboxService`).
        """
        payload = self._inbox.deliver(role, attempt_id)
        if not payload:
            return base_prompt
        return f"{payload}\n\n{base_prompt}"

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

    def _role_prompt_production(self, role: str, dsp) -> str:
        """Per-attempt instruction for one dispatched non-Lead role.

        This is a *single dispatched attempt*, not a fixed round-robin tick: Lead
        chose this role, and the role returns control to Lead by submitting
        exactly one structured handoff (there is no automatic next role). The
        triggering dispatch reason is supplied so the role does **delta
        discovery** first — live GitHub/repo state stays authoritative, but a full
        re-census is the fallback when the handoff context is stale or
        insufficient, not a mandatory step every attempt.
        """
        reason = (getattr(dsp, "reason", None) or "").strip()
        delta = (
            f"Lead dispatched you with this context: {reason}\n"
            if reason
            else "Lead dispatched you without extra context; discover the current delta.\n"
        )
        return (
            f"You are the `{role}` role, dispatched by Lead for ONE attempt on "
            f"`{self.cfg.target.remote}`. GOAL.md is at `{self.ws.goal_md}`.\n"
            f"{delta}"
            f"Any pending operator messages are delivered inline by the host at "
            f"the top of this prompt under an `OPERATOR INBOX` banner (you do not "
            f"read or clear inbox files yourself); honor them first, and treat an "
            f"`[OVERRIDE]` as taking precedence over GOAL.md and prior memory. Use "
            f"the dispatch context to check only what changed "
            f"since the triggering handoff (delta discovery); fall back to a full "
            f"review only if that context is stale or insufficient. Do your role's "
            f"work for this one attempt, then return control to Lead by calling "
            f"the `submit_role_handoff` tool EXACTLY ONCE with your structured "
            f"outcome (outcome_class=completed|no_progress, plus evidence, "
            f"changed, remaining, reason, and any disagreement or blocker). A "
            f"`completed` claim MUST carry concrete evidence and an explicit "
            f"changed/unchanged state account; a `no_progress` claim MUST carry a "
            f"return reason — an empty success-shaped payload is treated as a "
            f"protocol failure, not progress. Do not assume a "
            f"next role — routing is Lead's alone."
        )

    def _role_prompt(self, role: str, dsp) -> str:
        prompt = self._role_prompt_production(role, dsp)
        if self._prompt_policy is not None:
            return self._prompt_policy.decorate_role(role, prompt)
        return prompt

    def _lead_prompt(self, pending: list[HandoffView], run_id: str | None = None) -> str:
        prompt = self._lead_prompt_production(pending)
        # Surface the latest typed gate correction (#64) so Lead can repair the
        # public record or reroute with precise evidence rather than re-guessing.
        if run_id is not None:
            correction = self.disp.latest_gate_correction(run_id)
            if correction:
                prompt = (
                    f"{prompt}\n\nPUBLIC-BUS GATE CORRECTION (a prior routing "
                    f"decision was refused by the gate; the same task binding and "
                    f"pending handoffs are preserved and no attempt was reserved — "
                    f"repair the public record or reroute):\n{correction}"
                )
        if self._prompt_policy is not None:
            return self._prompt_policy.decorate_lead(pending, prompt)
        return prompt

    def _lead_prompt_production(self, pending: list[HandoffView]) -> str:
        ids = [h.id for h in pending]
        if pending:
            blocks = []
            for h in pending:
                lines = [
                    f"  - {h.id}: role={h.role} outcome={h.outcome_class.value}",
                    f"      reason: {h.reason_returned or '(none)'}",
                    f"      evidence: {h.evidence or '(none)'}",
                    f"      changed: {h.changed or '(none)'}",
                    f"      remaining: {h.remaining or '(none)'}",
                    f"      disagreement: {h.disagreement or '(none)'}",
                    f"      blocker: {h.blocker or '(none)'}",
                ]
                blocks.append("\n".join(lines))
            pending_block = (
                "Pending handoffs awaiting your routing decision (each is the "
                "structured outcome the role returned):\n"
                + "\n".join(blocks)
                + "\n"
            )
        else:
            pending_block = "There are no pending handoffs (this is the run's first decision).\n"
        return (
            f"You are Lead for `{self.cfg.target.remote}`. GOAL.md is at "
            f"`{self.ws.goal_md}`.\n"
            f"{pending_block}"
            f"Ground your decision in these handoffs plus any live facts you "
            f"actually check. Decide what happens next and submit exactly one "
            f"decision via the `submit_lead_decision` tool. Your decision MUST "
            f"acknowledge exactly these handoff ids: {ids}. Valid kinds: dispatch "
            f"(with a configured role: {list(self.configured_roles)}), "
            f"wait (with an observable wake_condition — also use this if you need "
            f"another turn to plan; the host re-solicits you under a bounded "
            f"budget), pause (with a human-only human_blocker, reserved for a "
            f"genuine operator-only prerequisite), finish (with final_acceptance "
            f"evidence)."
        )

    # ── bookkeeping ──
    def _persist_cycle(self) -> None:
        # Cycle/state writes surface on failure rather than being swallowed, so a
        # broken durable write halts the run instead of continuing silently.
        self.goal_state.cycles = self._cycle
        self.goal_state.save(self.ws.goal_json)
        self.ws.write_cycle(self._cycle)

    def _exit(self, reason: str) -> int:
        self.ws.state_dir.mkdir(parents=True, exist_ok=True)
        self.ws.exit_reason_file.write_text(reason + "\n")
        console.print(f"[blue]exit-reason:[/] {reason}")
        if self._owns_dispatcher:
            self.disp.close()
        return 0


class _NoModel:
    model = "?"
