"""Operator diagnostic surface (#13).

A single, typed, point-in-time :class:`DiagnosticSnapshot` projection that answers
"what is this crew doing, and what is the safe next action?" from **durable**
state, so an operator can diagnose active / waiting / paused / failed / resumed /
completed runs without reading raw SDK payloads or secrets.

Design (per the #13 audit):

* **One authoritative source, layered by authority.** The dispatch journal
  (`dispatch.db`) is durable truth: goal epoch → latest run → routing authority /
  current attempt → latest handoff + Lead decision, read in a *single*
  transaction via :meth:`crewd.dispatcher.Dispatcher.read_run_diagnostics`. Only
  then do we annotate it with explicitly **lower-authority** facts — daemon PID
  liveness, STOPPED/PAUSED sentinels, the `exit-reason` report artifact, and
  session taint. Controls/liveness never *substitute* for journal status; they
  annotate it.
* **Detect contradictions, don't silently pick one.** A transition can leave the
  controls and the journal briefly disagreeing (e.g. a dead daemon with an
  ``active`` run and a ``started`` orphan). We surface such contradictions and
  route the operator to ``crewd doctor`` rather than guessing.
* **Finite ``safe_next_action``.** The recommended action is derived from a
  finite state matrix (:class:`NextAction`), not ad-hoc prose, so it is testable
  from persisted fixtures.
* **Read-only.** Building a snapshot never mutates workspace or journal state.
* **Bounded, redacted handoff display.** Role-supplied handoff text is free-form
  and destined for both status and Lead prompts; it is redacted and length-bound,
  and shown as field *presence* by default, with an opt-in bounded detail view.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from .dispatcher import (
    LEAD_PENDING,
    Dispatcher,
    HandoffView,
    RunDiagnostics,
    RunStatus,
)
from .session_backend import TaintStore, redact
from .workspace import Workspace

# Maximum characters of any redacted free-text field surfaced to an operator.
_MAX_FIELD_CHARS = 240


class NextAction(str, enum.Enum):
    """The finite set of recommended operator actions."""

    NO_JOURNAL = "no_journal"            # nothing has run yet
    RUNNING = "running"                  # a live daemon/attempt is progressing
    CONTINUE = "continue"                # active but idle → `crewd run`
    RESUME_ORPHAN = "resume_orphan"      # crashed in-flight attempt → `crewd resume`
    WAIT = "wait"                        # waiting on a wake condition
    RESOLVE_BLOCKER = "resolve_blocker"  # paused on a human blocker
    RESUME = "resume"                    # interrupted/stopped → `crewd resume`
    NEW_GOAL = "new_goal"                # finished/exhausted → new epoch
    DOCTOR = "doctor"                    # contradictory state → `crewd doctor`


def _bound(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_FIELD_CHARS:
        return text
    return text[: _MAX_FIELD_CHARS - 1] + "…"


def _safe_text(value: Optional[str]) -> str:
    """Redact secrets then length-bound a free-text field for display."""
    if not value:
        return ""
    return _bound(redact(value))


def _summarize_handoff(h: Optional[HandoffView], *, detail: bool = False) -> Optional[dict]:
    """Bounded, redacted summary of a handoff.

    By default each free-text field is reported as presence + length only; the
    system-derived ``reason`` is always redacted + bounded (it is the primary
    routing signal). ``detail=True`` additionally includes bounded redacted text
    for every field. SDK payloads / arbitrary event JSON are never included.
    """
    if h is None:
        return None

    def _field(value: Optional[str]) -> dict:
        if not value:
            return {"present": False}
        out = {"present": True, "chars": len(value)}
        if detail:
            out["text"] = _safe_text(value)
        return out

    return {
        "id": h.id,
        "role": h.role,
        "outcome_class": h.outcome_class.value,
        "reason": _safe_text(h.reason_returned),
        "evidence": _field(h.evidence),
        "changed": _field(h.changed),
        "remaining": _field(h.remaining),
        "disagreement": _field(h.disagreement),
        "blocker": _field(h.blocker),
        "consumed": h.consumed_by_dispatch_id is not None,
    }


@dataclass(frozen=True)
class DiagnosticSnapshot:
    """A point-in-time operator view. Durable journal facts + lower-authority
    control/liveness/session annotations + a derived safe next action."""

    # identity / config
    workspace_root: str
    crew_name: str
    backend: str
    goal_label: Optional[str]

    # durable journal truth (None when no run exists for the goal label)
    run_id: Optional[str] = None
    run_status: Optional[str] = None
    routing_authority: Optional[str] = None          # 'lead_pending' or a dispatch id
    authority_holder: Optional[str] = None           # 'lead' | 'role' | None
    consecutive_unproductive: int = 0
    invalid_solicitations: int = 0
    wake_condition: Optional[str] = None
    human_blocker: Optional[str] = None
    pending_handoff_count: int = 0

    current_attempt: Optional[dict] = None           # in-flight attempt (role/state/session/gen)
    latest_handoff: Optional[dict] = None            # bounded, redacted summary
    latest_decision: Optional[dict] = None           # latest Lead decision (kind/role/reason)

    # lower-authority annotations (controls / liveness / session)
    daemon_pid: Optional[int] = None
    daemon_alive: bool = False
    stopped: bool = False
    paused_reason: Optional[str] = None
    exit_reason: Optional[str] = None
    current_session_tainted: bool = False

    # public-write durability + operator inbox delivery (issue #29 observability)
    public_writes: Optional[dict] = None             # {'pending': n, 'verified': n, 'pending_ids': [...]}
    inbox: Optional[dict] = None                     # per-role {'pending','delivering','processed'}
    recovery_action: Optional[str] = None            # operator-facing recovery hint, if any

    # derived
    contradictions: list[str] = field(default_factory=list)
    next_action: NextAction = NextAction.NO_JOURNAL
    next_action_detail: str = ""

    def to_dict(self) -> dict:
        """Stable machine-readable form (same projection as the human view)."""
        return {
            "workspace_root": self.workspace_root,
            "crew_name": self.crew_name,
            "backend": self.backend,
            "goal_label": self.goal_label,
            "run": None if self.run_id is None else {
                "id": self.run_id,
                "status": self.run_status,
                "routing_authority": self.routing_authority,
                "authority_holder": self.authority_holder,
                "consecutive_unproductive": self.consecutive_unproductive,
                "invalid_solicitations": self.invalid_solicitations,
                "wake_condition": self.wake_condition,
                "human_blocker": self.human_blocker,
                "pending_handoff_count": self.pending_handoff_count,
            },
            "current_attempt": self.current_attempt,
            "latest_handoff": self.latest_handoff,
            "latest_decision": self.latest_decision,
            "public_writes": self.public_writes,
            "inbox": self.inbox,
            "recovery_action": self.recovery_action,
            "controls": {
                "daemon_pid": self.daemon_pid,
                "daemon_alive": self.daemon_alive,
                "stopped": self.stopped,
                "paused_reason": self.paused_reason,
                "exit_reason": self.exit_reason,
                "current_session_tainted": self.current_session_tainted,
            },
            "contradictions": list(self.contradictions),
            "next_action": self.next_action.value,
            "next_action_detail": self.next_action_detail,
        }


def _detect_contradictions(diag: RunDiagnostics, *, stopped: bool, daemon_alive: bool,
                           exit_reason: Optional[str]) -> list[str]:
    """High-signal contradictions between durable journal and lower-authority controls."""
    out: list[str] = []
    status = diag.run.status
    if stopped and daemon_alive:
        out.append(
            "STOPPED sentinel is set but the daemon process is still alive — "
            "the stop has not taken effect yet."
        )
    if daemon_alive and status in (RunStatus.FINISHED, RunStatus.EXHAUSTED):
        out.append(
            f"daemon process is alive but the run is durably {status.value} "
            "(terminal) — a process is running past a finished run."
        )
    if exit_reason in ("goal-complete", "exhausted") and status == RunStatus.ACTIVE:
        out.append(
            f"exit-reason report says '{exit_reason}' but the run is still "
            "durably active — the exit-reason artifact is stale."
        )
    return out


def _derive_next_action(diag: RunDiagnostics, *, daemon_alive: bool, tainted: bool,
                        contradictions: list[str]) -> tuple[NextAction, str]:
    if contradictions:
        return (
            NextAction.DOCTOR,
            "Durable journal and controls disagree — run `crewd doctor` to reconcile "
            "before resuming.",
        )
    run = diag.run
    status = run.status
    if status == RunStatus.ACTIVE:
        if diag.current_attempt is not None:
            role = diag.current_attempt.role
            if daemon_alive:
                return (
                    NextAction.RUNNING,
                    f"A {role} attempt is in flight under a live daemon — monitor "
                    f"with `crewd logs`.",
                )
            taint_note = (
                " Its session is tainted, so recovery will advance to a fresh "
                "generation." if tainted else ""
            )
            return (
                NextAction.RESUME_ORPHAN,
                f"An orphaned {role} attempt is still marked in-flight after the "
                f"runner stopped — run `crewd run`; startup reconciliation "
                f"taints-before-finalizes the orphan, then continues with a fresh "
                f"generation." + taint_note,
            )
        if daemon_alive:
            return (
                NextAction.RUNNING,
                "Lead holds routing authority under a live daemon — running.",
            )
        return (NextAction.CONTINUE, "Active and idle — run `crewd run` to continue.")
    if status == RunStatus.WAITING:
        return (
            NextAction.WAIT,
            f"Waiting for: {run.wake_condition or 'an external wake condition'} — "
            f"`crewd resume` to force continue.",
        )
    if status == RunStatus.PAUSED:
        return (
            NextAction.RESOLVE_BLOCKER,
            f"Paused on a human blocker: {run.human_blocker or 'input required'} — "
            f"resolve it, then `crewd resume`.",
        )
    if status == RunStatus.INTERRUPTED:
        return (NextAction.RESUME, "Interrupted — resumable with `crewd resume`.")
    if status == RunStatus.STOPPED:
        return (NextAction.RESUME, "Stopped — resumable with `crewd resume`.")
    if status == RunStatus.EXHAUSTED:
        return (
            NextAction.NEW_GOAL,
            "Work budget exhausted — not resumable; start a new epoch with "
            "`crewd new-goal`.",
        )
    if status == RunStatus.FINISHED:
        return (
            NextAction.NEW_GOAL,
            "Goal complete — not resumable; start a new epoch with `crewd new-goal`.",
        )
    return (NextAction.DOCTOR, f"Unrecognized run status {status!r} — run `crewd doctor`.")


def _public_write_state(ws: Workspace) -> Optional[dict]:
    """Durable public-write journal counts (offline; reads state, no GitHub).

    Beyond raw counts this surfaces, per still-pending terminal write, the target
    (issue/pull + number), the retry/attempt count, and the last route — so an
    operator can tell a transient WAIT that the next ``crewd run`` reconciles
    automatically apart from a real human blocker that needs intervention (#49).
    """
    try:
        from .public_writer import IntentStore

        store = IntentStore.for_workspace(ws)
        counts = store.counts()
        if counts["pending"] == 0 and counts["verified"] == 0:
            return None
        pending = store.list_pending()
        pending_ids = [i.correlation_id for i in pending]
        pending_detail = [
            {
                "id": i.correlation_id,
                "target": f"{i.target}#{i.number}",
                "attempts": i.attempts,
                "route": i.last_route or "reserved",
            }
            for i in pending
        ]
        # A pending write that last routed to PAUSE (permission) or REJECT (an
        # invalid public record that will not fix itself on retry) is genuinely
        # operator-needed; a WAIT/reserved write self-heals on the next reconcile
        # (issue #49).
        needs_operator = sorted(
            i.correlation_id for i in pending
            if i.last_route in ("pause", "reject")
        )
        return {
            "pending": counts["pending"],
            "verified": counts["verified"],
            "pending_ids": pending_ids,
            "pending_detail": pending_detail,
            "needs_operator": needs_operator,
        }
    except Exception:
        return None


def _inbox_state(ws: Workspace) -> Optional[dict]:
    """Per-role operator-inbox delivery counts (content never surfaced)."""
    try:
        from .github_bus import ROLES
        from .inbox import InboxService

        svc = InboxService.for_workspace(ws)
        out = {}
        for role in ROLES:
            counts = svc.counts(role)
            if any(counts.values()):
                out[role] = counts
        return out or None
    except Exception:
        return None


def _recovery_hint(public_writes: Optional[dict], inbox: Optional[dict]) -> Optional[str]:
    """A single operator-facing recovery hint derived from durable side-channels."""
    hints = []
    if public_writes and public_writes.get("pending"):
        n = public_writes["pending"]
        needs_operator = public_writes.get("needs_operator") or []
        if needs_operator:
            hints.append(
                f"{len(needs_operator)} public write(s) need a genuine operator "
                f"action (permission/policy denial or an invalid public record): "
                f"{', '.join(needs_operator)} — resolve it, then `crewd resume`."
            )
        self_heal = n - len(needs_operator)
        if self_heal > 0:
            hints.append(
                f"{self_heal} public write(s) reserved but unverified — a "
                "`crewd run` reconciles them automatically once GitHub is "
                "reachable (no operator action needed)."
            )
    if inbox:
        delivering = sum(v.get("delivering", 0) for v in inbox.values())
        if delivering:
            hints.append(
                f"{delivering} operator message(s) staged for an in-flight attempt "
                "— they will be archived once the attempt terminalises."
            )
    return " ".join(hints) or None


def build_snapshot(ws: Workspace, *, crew_name: str, backend: str,
                   goal_label: Optional[str]) -> DiagnosticSnapshot:
    """Build the operator diagnostic snapshot. Never mutates any state."""
    exit_reason = None
    if ws.exit_reason_file.exists():
        exit_reason = ws.exit_reason_file.read_text(errors="replace").strip() or None
    pid = ws.read_pid()
    daemon_alive = ws.is_daemon_alive()
    stopped = ws.is_stopped()
    paused_reason = ws.pause_reason()

    base = dict(
        workspace_root=str(ws.root),
        crew_name=crew_name,
        backend=backend,
        goal_label=goal_label,
        daemon_pid=pid,
        daemon_alive=daemon_alive,
        stopped=stopped,
        paused_reason=paused_reason,
        exit_reason=exit_reason,
        public_writes=_public_write_state(ws),
        inbox=_inbox_state(ws),
    )
    base["recovery_action"] = _recovery_hint(base["public_writes"], base["inbox"])

    diag = _read_run_diagnostics(ws, goal_label)
    if diag is None:
        # No durable run for this label yet. Controls still carry authority-0
        # signal, and must not be silently ignored (#13 precedence): a *live*
        # daemon with no journal is a genuine contradiction — never advise
        # starting a second run over a running process.
        if daemon_alive:
            contradiction = (
                f"a daemon process (PID {pid}) is alive but no dispatch journal "
                "exists for this goal — it may be mid-startup, or the PID is "
                "stale/foreign; do not start another run until this is diagnosed."
            )
            return DiagnosticSnapshot(
                **base,
                contradictions=[contradiction],
                next_action=NextAction.DOCTOR,
                next_action_detail=(
                    "A daemon PID is alive but nothing has been journaled — inspect "
                    "with `crewd doctor` / `crewd logs` before starting another run."
                ),
            )
        detail = "No dispatch journal for this goal yet — run `crewd run` to start."
        if pid is not None:
            # A dead PID file is a stale control artifact, not a run. Starting is
            # still the safe action; point at `doctor` to clear the leftover.
            detail += (
                f" (A stale daemon PID file is present, PID {pid} not alive; "
                "`crewd doctor` clears it.)"
            )
        return DiagnosticSnapshot(
            **base,
            next_action=NextAction.NO_JOURNAL,
            next_action_detail=detail,
        )

    tainted = _current_session_tainted(ws, diag)
    contradictions = _detect_contradictions(
        diag, stopped=stopped, daemon_alive=daemon_alive, exit_reason=exit_reason
    )
    action, detail = _derive_next_action(
        diag, daemon_alive=daemon_alive, tainted=tainted, contradictions=contradictions
    )

    run = diag.run
    current_attempt = None
    if diag.current_attempt is not None:
        a = diag.current_attempt
        current_attempt = {
            "role": a.role,
            "state": a.state.value,
            "session_id": a.session_id,
            "generation": a.generation,
            "tainted": tainted,
        }
    latest_decision = None
    if diag.latest_dispatch is not None:
        d = diag.latest_dispatch
        latest_decision = {
            "kind": d.kind.value,
            "role": d.role,
            "reason": _safe_text(d.reason),
        }

    return DiagnosticSnapshot(
        **base,
        run_id=run.id,
        run_status=run.status.value,
        routing_authority=run.routing_authority,
        authority_holder=("lead" if run.routing_authority == LEAD_PENDING else "role"),
        consecutive_unproductive=run.consecutive_unproductive,
        invalid_solicitations=run.invalid_solicitations,
        wake_condition=run.wake_condition,
        human_blocker=run.human_blocker,
        pending_handoff_count=diag.pending_handoff_count,
        current_attempt=current_attempt,
        latest_handoff=_summarize_handoff(diag.latest_handoff),
        latest_decision=latest_decision,
        current_session_tainted=tainted,
        contradictions=contradictions,
        next_action=action,
        next_action_detail=detail,
    )


def _read_run_diagnostics(ws: Workspace, goal_label: Optional[str]) -> Optional[RunDiagnostics]:
    db = ws.state_dir / "dispatch.db"
    if goal_label is None or not db.exists():
        return None
    disp = Dispatcher(db)
    try:
        return disp.read_run_diagnostics(goal_label)
    finally:
        disp.close()


def _current_session_tainted(ws: Workspace, diag: RunDiagnostics) -> bool:
    att = diag.current_attempt
    if att is None or not att.session_id:
        return False
    store = TaintStore(ws.role_cfg_dir(att.role) / ".crewd-sdk-taint")
    return store.is_tainted(att.session_id)
