"""Typed attempt-execution seam between the dispatcher and the SDK transport.

The dispatcher kernel (:mod:`crewd.dispatcher`) reserves work slots and records
terminal outcomes, but it must not know *how* an attempt runs. The orchestrator
(:mod:`crewd.orchestrator`) drives the kernel and delegates the actual work of
running a role tick — or a Lead decision turn — to an :class:`AttemptExecutor`.

This module defines that seam:

* :class:`AttemptRequest` — everything needed to run one attempt.
* :class:`RoleAttemptOutcome` / :class:`LeadTurnOutcome` — the *typed* results
  the orchestrator consumes directly (an :class:`~crewd.session_backend.AttemptResult`
  plus the durable session identity, and — for a Lead turn — the untrusted
  candidate :class:`~crewd.dispatcher.LeadDecision` captured from the turn).
* :class:`AttemptExecutor` — the protocol the orchestrator depends on.
* :class:`SdkAttemptExecutor` — the production implementation over the real
  ``github-copilot-sdk`` transport (via :mod:`crewd.sdk_adapter`), reusing the
  fail-closed workspace mounting and goal-epoch session identity established in
  #10. Deterministic tests inject a fake executor instead.

The orchestrator therefore consumes ``AttemptResult.outcome`` directly rather
than reconstructing lifecycle meaning from a process exit code — that reversal
of the #10 exit-code mapping is the point of this seam.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from .dispatcher import HandoffOutcome, LeadDecision, classify
from .session_backend import AttemptOutcome, AttemptResult, CancelToken

# Callback the orchestrator supplies to durably journal an attempt as `started`
# with its selected session identity BEFORE any SDK send. Called at most once,
# after session selection and before the transport runs. Raising aborts the
# attempt before any SDK work (the reservation is later reconciled on restart).
OnStarted = Callable[[str, int], None]


class _SingleSubmitCapture:
    """Attempt-local, thread-safe *exactly-one* submission capture.

    Enforces the accepted #17 invariant that a turn submits *exactly one*
    structured payload through its SDK custom tool: zero **or multiple**
    submissions are invalid. The handler calls :meth:`submit` (which never
    mutates durable dispatcher state); the executor reads :meth:`result` after
    the turn, obtaining the single payload only when exactly one submission
    occurred. The official SDK may execute custom tools concurrently, so all
    access is lock-guarded and the durable fact that multiple calls occurred is
    preserved in :attr:`count`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._first = None

    def submit(self, candidate) -> bool:
        """Record one submission; return ``True`` iff it is the first (accepted).

        Subsequent submissions are rejected (``False``) but still counted, so a
        double-submit resolves as invalid rather than last-wins.
        """
        with self._lock:
            self._count += 1
            if self._count == 1:
                self._first = candidate
                return True
            return False

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def result(self):
        """The single candidate iff exactly one submission occurred, else None."""
        with self._lock:
            return self._first if self._count == 1 else None


class LeadDecisionCapture(_SingleSubmitCapture):
    """Exactly-one capture for the ``submit_lead_decision`` (Lead turn) tool."""


class RoleHandoffCapture(_SingleSubmitCapture):
    """Exactly-one capture for the ``submit_role_handoff`` (non-Lead tick) tool.

    A dispatched role returns its structured outcome to Lead through this single
    channel. Semantics mirror :class:`LeadDecisionCapture`: zero or multiple
    submissions are a protocol failure that must NOT resolve as a completion.
    """


@dataclass(frozen=True)
class AttemptRequest:
    """All inputs required to execute one attempt (role tick or Lead turn)."""

    role: str
    model: str
    prompt: str
    config_dir: Path
    add_dirs: list[Path]
    cwd: Path
    workspace_root: Path
    goal_label: str
    timeout: float
    log_path: Path


ROLE_CLAIMABLE_OUTCOMES = ("completed", "no_progress")


@dataclass(frozen=True)
class RoleHandoff:
    """Structured outcome a non-Lead role returns to Lead for one attempt.

    Captured (untrusted) from the ``submit_role_handoff`` SDK custom tool. The
    dispatcher/orchestrator — never this payload — decide the routing class: on a
    clean idle turn the role may *claim* only ``completed`` vs ``no_progress``
    (:data:`ROLE_CLAIMABLE_OUTCOMES`); the transport lifecycle outcome overrides
    any claim otherwise. ``disagreement`` and ``blocker`` are evidence for Lead's
    routing, never routing authority.
    """

    outcome_class: str
    evidence: str = ""
    changed: str = ""
    remaining: str = ""
    reason: str = ""
    disagreement: str = ""
    blocker: str = ""


@dataclass(frozen=True)
class RoleAttemptOutcome:
    """Typed result of executing one role tick."""

    result: AttemptResult
    session_id: str
    generation: int
    mount_error: Optional[str] = None  # set iff the attempt was refused pre-execution
    # Structured return captured from the role's ``submit_role_handoff`` tool
    # (``None`` when the role submitted nothing) and the raw submission count, so
    # the orchestrator can distinguish a valid single handoff from a zero/multiple
    # protocol failure that must not resolve as a completion.
    handoff: Optional[RoleHandoff] = None
    handoff_submissions: int = 0


@dataclass(frozen=True)
class LeadTurnOutcome:
    """Typed result of one Lead decision turn.

    ``decision`` is the *untrusted* candidate captured from the Lead turn (e.g.
    via the ``submit_lead_decision`` SDK custom tool). It is ``None`` when the
    turn produced no decision (timeout, error, cancel, or no tool call). The
    dispatcher — never this executor — decides whether to apply it, via
    :meth:`crewd.dispatcher.Dispatcher.resolve_lead_solicitation`.
    """

    result: AttemptResult
    session_id: str
    generation: int
    decision: Optional[LeadDecision]
    mount_error: Optional[str] = None


class AttemptExecutor(Protocol):
    """Port the orchestrator drives to run role ticks and Lead turns."""

    def doctor(self) -> list[str]:
        """Return a list of health-check error strings; empty means healthy."""
        ...

    def execute_role(
        self,
        req: AttemptRequest,
        *,
        on_started: Optional[OnStarted] = None,
        cancel: "Optional[CancelToken]" = None,
    ) -> RoleAttemptOutcome:
        """Run one role tick, returning its typed outcome (never raises for a
        normal SDK failure — that is encoded in ``result.outcome``).

        ``on_started`` is invoked with the selected ``(session_id, generation)``
        after session selection but BEFORE any SDK send, so the attempt's session
        identity is durably journaled before transport work begins. It is not
        called when the attempt is refused pre-execution (a mounting error): no
        session is opened and no send occurs.

        ``cancel`` is an optional :class:`~crewd.session_backend.CancelToken` the
        orchestrator may trip (from its control/signal poll) to cancel the attempt
        while it is in flight. A clean, confirmed cancellation yields
        :attr:`~crewd.session_backend.AttemptOutcome.CANCELLED_CLEAN`; an
        unconfirmed one taints the session.
        """
        ...

    def run_lead(
        self,
        req: AttemptRequest,
        *,
        on_started: Optional[OnStarted] = None,
        cancel: "Optional[CancelToken]" = None,
    ) -> LeadTurnOutcome:
        """Run one Lead decision turn, returning its typed outcome + candidate.

        ``on_started`` has the same pre-send journaling contract as
        :meth:`execute_role`; ``cancel`` the same in-flight cancellation contract.
        """
        ...


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


# Terminal outcome used when a session error prevented the attempt from even
# starting (fail-closed mounting, taint on resume). Mirrors run_attempt's own
# error classification so the orchestrator sees a uniform AttemptResult.
def _error_result(req: AttemptRequest, session_id: str, message: str) -> AttemptResult:
    return AttemptResult(
        attempt_id="",
        session_id=session_id,
        role=req.role,
        outcome=AttemptOutcome.SDK_ERROR,
        duration=0.0,
        error=message,
        cleanup_confirmed=True,
    )


class SdkAttemptExecutor:
    """Production :class:`AttemptExecutor` over the real SDK transport.

    Reuses the #10 session machinery: fail-closed workspace mounting (the SDK
    exposes a single ``working_directory`` and no ``--add-dir`` equivalent),
    goal-epoch/recovery-generation session identity, taint tracking, and the
    :func:`~crewd.session_backend.run_attempt` lifecycle state machine.

    ``ops_factory`` and ``lead_ops_factory`` are injectable so the whole
    selectable path (not just the domain state machine) is covered by
    deterministic tests without the SDK. When ``lead_ops_factory`` is not given,
    the Lead turn reuses the ordinary ops factory and captures no decision (the
    real ``submit_lead_decision`` custom-tool wiring lives in the adapter and is
    exercised by the bounded live smoke — see docs/sdk-backend.md).
    """

    def __init__(self, ops_factory=None, lead_ops_factory=None):
        self._ops_factory = ops_factory
        self._lead_ops_factory = lead_ops_factory

    def doctor(self) -> list[str]:
        from .sdk_adapter import sdk_available

        errs: list[str] = []
        if self._ops_factory is None and not sdk_available():
            errs.append(
                "`github-copilot-sdk` (import `copilot`) is not importable, but it "
                "is a required dependency of crewd for the default `backend: "
                "copilot-sdk`. Repair the existing install so its declared "
                "dependencies are present — e.g. `uv sync` (repo checkout), "
                "`pip install --upgrade --force-reinstall crewd` (pip), or "
                "`uv tool install --reinstall crewd` (uv tool) — then run "
                "`crewd doctor`."
            )
        return errs

    # ── public seam ─────────────────────────────────────────────
    def execute_role(
        self,
        req: AttemptRequest,
        *,
        on_started: Optional[OnStarted] = None,
        cancel: Optional[CancelToken] = None,
    ) -> RoleAttemptOutcome:
        session_id, generation, mount_err, run = self._run(req, lead=False, cancel=cancel)
        if mount_err is not None:
            # No session opened and no SDK send — nothing to journal as started.
            return RoleAttemptOutcome(
                result=_error_result(req, session_id, mount_err),
                session_id=session_id,
                generation=generation,
                mount_error=mount_err,
            )
        if on_started is not None:
            # Durably persist the selected session identity BEFORE any SDK send.
            # A raised error here aborts the attempt before transport work; it is
            # deliberately NOT swallowed so persistence failure surfaces.
            on_started(session_id, generation)
        result, handoff, submissions = run()
        return RoleAttemptOutcome(
            result=result,
            session_id=session_id,
            generation=generation,
            handoff=handoff,
            handoff_submissions=submissions,
        )

    def run_lead(
        self,
        req: AttemptRequest,
        *,
        on_started: Optional[OnStarted] = None,
        cancel: Optional[CancelToken] = None,
    ) -> LeadTurnOutcome:
        session_id, generation, mount_err, run = self._run(req, lead=True, cancel=cancel)
        if mount_err is not None:
            return LeadTurnOutcome(
                result=_error_result(req, session_id, mount_err),
                session_id=session_id,
                generation=generation,
                decision=None,
                mount_error=mount_err,
            )
        if on_started is not None:
            on_started(session_id, generation)
        result, decision = run()
        return LeadTurnOutcome(
            result=result,
            session_id=session_id,
            generation=generation,
            decision=decision,
        )

    # ── shared machinery ────────────────────────────────────────
    def _run(self, req: AttemptRequest, *, lead: bool, cancel: Optional[CancelToken] = None):
        """Resolve session identity + mounting, returning a deferred runner.

        Returns ``(session_id, generation, mount_error, runner)``. ``runner`` is
        a zero-arg callable that actually drives the attempt (so a mounting
        refusal never opens a session). For a Lead turn the runner returns
        ``(result, decision)``; for a role tick it returns ``result``.
        """
        from .session_backend import (
            AttemptConfig,
            SessionRegistry,
            TaintStore,
            TaintedSessionError,
            run_attempt,
        )

        req.config_dir.mkdir(parents=True, exist_ok=True)
        req.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_lines: list[str] = []

        ws_root = Path(req.workspace_root).resolve()
        required = [Path(req.cwd).resolve()] + [Path(d).resolve() for d in req.add_dirs]
        unmountable = [d for d in required if not _is_within(d, ws_root)]
        if unmountable:
            msg = (
                f"backend=copilot-sdk role={req.role} PRE-EXECUTION ERROR: the "
                f"following required/configured paths are outside the workspace "
                f"root ({ws_root}) and cannot be mounted (the SDK has no "
                f"`--add-dir` equivalent; only a single working_directory is "
                f"available): " + ", ".join(str(d) for d in unmountable) + ". "
                f"Refusing to start the SDK session so the role is not launched "
                f"without required context."
            )
            log_lines.append("[crewd] " + msg)
            self._write_log(req.log_path, log_lines)
            gen = 0
            sid = f"crewd-{req.role}-unmounted"
            return sid, gen, msg, None

        working_dir = ws_root
        taint_store = TaintStore(req.config_dir / ".crewd-sdk-taint")
        registry = SessionRegistry(
            req.config_dir / ".crewd-sdk-sessions.json", workspace_id=str(ws_root)
        )
        decision = registry.decide(
            goal_label=req.goal_label, role=req.role, taint_store=taint_store
        )
        session_id = decision.session_id
        resume = decision.resume

        log_lines.append(
            f"[crewd] backend=copilot-sdk role={req.role} model={req.model} "
            f"goal={req.goal_label or '(none)'} session={session_id} "
            f"gen={decision.generation} resume={resume} working_dir={working_dir}"
        )
        if decision.fresh_reason:
            log_lines.append(f"[crewd] fresh session: {decision.fresh_reason}")

        capture = LeadDecisionCapture() if lead else None
        role_capture = RoleHandoffCapture() if not lead else None
        if lead:
            ops = self._make_lead_ops(
                session_id=session_id,
                role=req.role,
                model=req.model,
                config_dir=req.config_dir,
                working_dir=working_dir,
                capture=capture,
            )
        else:
            ops = self._make_ops(
                session_id=session_id,
                role=req.role,
                model=req.model,
                config_dir=req.config_dir,
                working_dir=working_dir,
                role_capture=role_capture,
            )

        cfg = AttemptConfig(wait_timeout=float(req.timeout))

        def runner():
            nonlocal log_lines
            try:
                result = run_attempt(
                    ops, taint_store, prompt=req.prompt, resume=resume, config=cfg,
                    cancel=cancel,
                )
            except TaintedSessionError as e:
                log_lines.append(f"[crewd] {e}")
                self._write_log(req.log_path, log_lines)
                res = AttemptResult(
                    attempt_id="",
                    session_id=session_id,
                    role=req.role,
                    outcome=AttemptOutcome.TAINTED,
                    duration=0.0,
                    error=str(e),
                    cleanup_confirmed=False,
                )
                return (res, None) if lead else (res, None, 0)

            for ev in result.events:
                log_lines.append(ev.to_line())
            for s in result.session_event_summaries:
                log_lines.append(f"  sdk-event: {s}")
            log_lines.append(
                f"[crewd] outcome={result.outcome.value} "
                f"duration={result.duration:.3f}s tainted={result.tainted} "
                f"cleanup_confirmed={result.cleanup_confirmed}"
            )
            self._write_log(req.log_path, log_lines)
            self._write_sidecar(req, result, decision.generation)

            if lead:
                candidate = self._read_captured_decision(capture, ops)
                return result, candidate
            handoff = self._read_captured_handoff(role_capture)
            submissions = role_capture.count if role_capture is not None else 0
            return result, handoff, submissions

        return session_id, decision.generation, None, runner

    # ── ops factories ───────────────────────────────────────────
    def _make_ops(self, *, session_id, role, model, config_dir, working_dir, role_capture=None):
        if self._ops_factory is not None:
            return self._ops_factory(
                session_id=session_id, role=role, model=model,
                config_dir=config_dir, working_dir=working_dir,
            )
        from .sdk_adapter import SdkRoleRuntime

        # Register the submit_role_handoff custom tool so the dispatched role can
        # return a structured outcome to Lead. The handler only records the
        # (untrusted) payload into ``role_capture`` (attempt-local memory); it
        # never mutates durable dispatcher state — the single consuming
        # transaction is record_terminal.
        return SdkRoleRuntime(
            session_id=session_id, role=role, model=model,
            config_dir=config_dir, working_dir=working_dir,
            role_handoff_capture=role_capture,
        )

    def _make_lead_ops(self, *, session_id, role, model, config_dir, working_dir, capture):
        if self._lead_ops_factory is not None:
            return self._lead_ops_factory(
                session_id=session_id, role=role, model=model,
                config_dir=config_dir, working_dir=working_dir, capture=capture,
            )
        from .sdk_adapter import SdkRoleRuntime

        # Real SDK path: register the submit_lead_decision custom tool so the
        # Lead turn can hand back a structured candidate. The tool handler only
        # records the candidate into ``capture`` (attempt-local memory); it must
        # NOT mutate any durable dispatcher state — the single consuming
        # transaction is resolve_lead_solicitation. Live wiring of the custom
        # tool is exercised by the bounded live smoke (docs/sdk-backend.md).
        return SdkRoleRuntime(
            session_id=session_id, role=role, model=model,
            config_dir=config_dir, working_dir=working_dir,
            lead_decision_capture=capture,
        )

    @staticmethod
    def _read_captured_decision(capture: "LeadDecisionCapture", ops) -> Optional[LeadDecision]:
        # Exactly-one submission: capture.result() is the single candidate, or
        # None when zero or multiple decisions were submitted (both invalid). The
        # dispatcher still applies its own semantic guards on top of this.
        raw = capture.result() if capture is not None else None
        if raw is None:
            return None
        if isinstance(raw, LeadDecision):
            return raw
        try:
            return parse_lead_decision(raw)
        except Exception:
            return None

    @staticmethod
    def _read_captured_handoff(capture: "Optional[RoleHandoffCapture]") -> Optional[RoleHandoff]:
        # Exactly-one submission: capture.result() is the single payload, or None
        # when zero or multiple were submitted (both a protocol failure). The
        # orchestrator resolves the terminal class (transport-authoritative).
        raw = capture.result() if capture is not None else None
        if raw is None:
            return None
        if isinstance(raw, RoleHandoff):
            return raw
        try:
            return parse_role_handoff(raw)
        except Exception:
            return None

    @staticmethod
    def _write_log(log_path: Path, lines: list[str]) -> None:
        log_path.write_text("\n".join(lines) + "\n")

    @staticmethod
    def _write_sidecar(req: AttemptRequest, result: AttemptResult, generation: int) -> None:
        sidecar = req.log_path.with_suffix(req.log_path.suffix + ".attempt.json")
        try:
            sidecar.write_text(
                json.dumps(
                    {
                        "attempt_id": result.attempt_id,
                        "session_id": result.session_id,
                        "role": result.role,
                        "goal_label": req.goal_label,
                        "generation": generation,
                        "outcome": result.outcome.value,
                        "duration": result.duration,
                        "tainted": result.tainted,
                        "cleanup_confirmed": result.cleanup_confirmed,
                        "error": result.error,
                        "events": [
                            {"phase": ev.phase.value, "elapsed": ev.elapsed, "detail": ev.detail}
                            for ev in result.events
                        ],
                    },
                    indent=2,
                )
            )
        except Exception:
            pass


def parse_lead_decision(payload) -> LeadDecision:
    """Parse an untrusted ``submit_lead_decision`` payload into a LeadDecision.

    The payload is whatever the Lead turn's tool call produced (a dict from the
    SDK custom tool). This performs *shape* validation only — it never trusts the
    payload semantically. All semantic guards (nonce unchanged, acks match the
    snapshot, target role configured) are enforced by
    :meth:`crewd.dispatcher.Dispatcher.resolve_lead_solicitation`.
    """
    from .dispatcher import DecisionKind

    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError(f"lead decision payload is not an object: {type(payload)!r}")

    kind_raw = payload.get("kind")
    kind = DecisionKind(kind_raw)  # raises ValueError on unknown kind
    ack = tuple(payload.get("ack_handoff_ids") or ())
    if kind is DecisionKind.DISPATCH:
        return LeadDecision.dispatch(payload["role"], ack=ack, reason=payload.get("reason"))
    if kind is DecisionKind.CONTINUE_LEAD:
        return LeadDecision.continue_lead(ack=ack, reason=payload.get("reason"))
    if kind is DecisionKind.WAIT:
        return LeadDecision.wait(payload["wake_condition"], ack=ack, reason=payload.get("reason"))
    if kind is DecisionKind.PAUSE:
        return LeadDecision.pause(payload["human_blocker"], ack=ack)
    if kind is DecisionKind.FINISH:
        return LeadDecision.finish(payload["final_acceptance"], ack=ack)
    raise ValueError(f"lead decision kind {kind!r} cannot be submitted by a Lead turn")


def parse_role_handoff(payload) -> RoleHandoff:
    """Parse an untrusted ``submit_role_handoff`` payload into a RoleHandoff.

    Shape validation only — it never trusts the payload semantically. The
    orchestrator decides the routing class transport-authoritatively via
    :func:`resolve_role_terminal`, so an out-of-range ``outcome_class`` here is
    tolerated (it simply cannot claim a completion later).
    """
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError(f"role handoff payload is not an object: {type(payload)!r}")

    def _s(key: str) -> str:
        v = payload.get(key)
        return v if isinstance(v, str) else ("" if v is None else str(v))

    return RoleHandoff(
        outcome_class=_s("outcome_class"),
        evidence=_s("evidence"),
        changed=_s("changed"),
        remaining=_s("remaining"),
        reason=_s("reason"),
        disagreement=_s("disagreement"),
        blocker=_s("blocker"),
    )


@dataclass(frozen=True)
class RoleTerminal:
    """Resolved routing terminal for one role attempt (transport-authoritative)."""

    outcome_class: HandoffOutcome
    evidence: str
    changed: str
    remaining: str
    reason_returned: str
    disagreement: str = ""
    blocker: str = ""


def _role_claim_defect(handoff: Optional[RoleHandoff], submissions: int) -> Optional[str]:
    """Return a protocol-failure detail for a clean-idle role claim, else ``None``.

    A clean idle turn may claim ``completed``/``no_progress`` only through
    *exactly one* well-formed submission that also carries a minimal semantic
    progress account, so a success-shaped but empty payload cannot silently reset
    the no-progress guard (a defect #9/#12 explicitly aim to remove). The account
    is role-neutral: ``completed`` needs concrete ``evidence`` **and** an explicit
    state statement (``changed`` — which may legitimately be ``none`` for a
    verifiable no-mutation outcome such as a Verifier approval or Advisory
    finding); ``no_progress`` needs a non-empty return ``reason``.
    """
    if submissions == 0:
        return "no submit_role_handoff call"
    if submissions > 1:
        return f"{submissions} submit_role_handoff calls (exactly one required)"
    if handoff is None:
        return "malformed submit_role_handoff payload"
    oc = handoff.outcome_class
    if oc not in ROLE_CLAIMABLE_OUTCOMES:
        return f"invalid outcome_class {oc!r}"
    if oc == "completed":
        if not handoff.evidence.strip():
            return "completed claim without concrete evidence"
        if not handoff.changed.strip():
            return "completed claim without an explicit changed/unchanged state account"
    else:  # no_progress
        if not handoff.reason.strip():
            return "no_progress claim without a return reason"
    return None


def resolve_role_terminal(
    result: AttemptResult,
    handoff: Optional[RoleHandoff],
    submissions: int,
) -> RoleTerminal:
    """Decide a role attempt's routing terminal, keeping the transport authoritative.

    The SDK lifecycle outcome always governs the *class* unless the turn reached
    a clean idle (:attr:`~crewd.session_backend.AttemptOutcome.IDLE_COMPLETED`):

    * Non-idle transport (error / wait-timeout / cancel / taint) overrides any
      success-shaped role claim — the role cannot upgrade a failed turn.
    * A clean idle turn lets the role's *single* structured handoff choose
      ``completed`` vs ``no_progress`` (:data:`ROLE_CLAIMABLE_OUTCOMES`) *only*
      when it carries a minimal semantic progress account (see
      :func:`_role_claim_defect`).
    * Zero, multiple, malformed, or under-substantiated submissions on a clean
      idle are a protocol failure: resolved as ``uncertain`` (which counts toward
      the no-progress bounds), never a silent completion. This path never
      dereferences a missing handoff.

    In every case the role's ``evidence``/``changed``/``disagreement``/``blocker``
    context is carried through when present, so Lead routes on the richest
    available information (as evidence, never as routing authority).
    """
    ev = handoff.evidence if handoff else ""
    changed = handoff.changed if handoff else ""
    remaining = handoff.remaining if handoff else ""
    disagreement = handoff.disagreement if handoff else ""
    blocker = handoff.blocker if handoff else ""

    if result.outcome is not AttemptOutcome.IDLE_COMPLETED:
        # Transport is authoritative: the lifecycle outcome wins the class.
        return RoleTerminal(
            outcome_class=classify(result.outcome),
            evidence=ev,
            changed=changed,
            remaining=result.error or remaining,
            reason_returned=f"sdk:{result.outcome.value}",
            disagreement=disagreement,
            blocker=blocker,
        )

    # Clean idle turn: the role's exactly-one, substantiated claim governs.
    detail = _role_claim_defect(handoff, submissions)
    if detail is not None:
        return RoleTerminal(
            outcome_class=HandoffOutcome.UNCERTAIN,
            evidence=ev,
            changed=changed,
            remaining=remaining or "role returned idle without a valid structured handoff",
            reason_returned=f"role_protocol_failure: {detail}",
            disagreement=disagreement,
            blocker=blocker,
        )

    claimed = (
        HandoffOutcome.COMPLETED
        if handoff.outcome_class == "completed"
        else HandoffOutcome.NO_PROGRESS
    )
    # ``reason_returned`` is the role's faithful return reason. ``disagreement``
    # and ``blocker`` are first-class fields carried separately (below) and are
    # rendered on their own lines everywhere they surface (the production Lead
    # prompt and the status projection). Do NOT fold them into ``reason`` — doing
    # so both corrupts the durable reason and double-renders those two fields.
    reason = handoff.reason or f"role:{handoff.outcome_class}"
    return RoleTerminal(
        outcome_class=claimed,
        evidence=ev,
        changed=changed,
        remaining=remaining,
        reason_returned=reason,
        disagreement=disagreement,
        blocker=blocker,
    )
