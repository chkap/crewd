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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

from .dispatcher import LeadDecision
from .session_backend import AttemptOutcome, AttemptResult


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


@dataclass(frozen=True)
class RoleAttemptOutcome:
    """Typed result of executing one role tick."""

    result: AttemptResult
    session_id: str
    generation: int
    mount_error: Optional[str] = None  # set iff the attempt was refused pre-execution


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

    def execute_role(self, req: AttemptRequest) -> RoleAttemptOutcome:
        """Run one role tick, returning its typed outcome (never raises for a
        normal SDK failure — that is encoded in ``result.outcome``)."""
        ...

    def run_lead(self, req: AttemptRequest) -> LeadTurnOutcome:
        """Run one Lead decision turn, returning its typed outcome + candidate."""
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
                "`github-copilot-sdk` (import `copilot`) not installed. "
                "Add it (`pip install 'crewd[sdk]'`) to use backend: copilot-sdk."
            )
        return errs

    # ── public seam ─────────────────────────────────────────────
    def execute_role(self, req: AttemptRequest) -> RoleAttemptOutcome:
        session_id, generation, mount_err, run = self._run(req, lead=False)
        if mount_err is not None:
            return RoleAttemptOutcome(
                result=_error_result(req, session_id, mount_err),
                session_id=session_id,
                generation=generation,
                mount_error=mount_err,
            )
        result = run()
        return RoleAttemptOutcome(
            result=result, session_id=session_id, generation=generation
        )

    def run_lead(self, req: AttemptRequest) -> LeadTurnOutcome:
        session_id, generation, mount_err, run = self._run(req, lead=True)
        if mount_err is not None:
            return LeadTurnOutcome(
                result=_error_result(req, session_id, mount_err),
                session_id=session_id,
                generation=generation,
                decision=None,
                mount_error=mount_err,
            )
        result, decision = run()
        return LeadTurnOutcome(
            result=result,
            session_id=session_id,
            generation=generation,
            decision=decision,
        )

    # ── shared machinery ────────────────────────────────────────
    def _run(self, req: AttemptRequest, *, lead: bool):
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

        capture: dict = {}
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
            )

        cfg = AttemptConfig(wait_timeout=float(req.timeout))

        def runner():
            nonlocal log_lines
            try:
                result = run_attempt(
                    ops, taint_store, prompt=req.prompt, resume=resume, config=cfg
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
                return (res, None) if lead else res

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
            return result

        return session_id, decision.generation, None, runner

    # ── ops factories ───────────────────────────────────────────
    def _make_ops(self, *, session_id, role, model, config_dir, working_dir):
        if self._ops_factory is not None:
            return self._ops_factory(
                session_id=session_id, role=role, model=model,
                config_dir=config_dir, working_dir=working_dir,
            )
        from .sdk_adapter import SdkRoleRuntime

        return SdkRoleRuntime(
            session_id=session_id, role=role, model=model,
            config_dir=config_dir, working_dir=working_dir,
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
    def _read_captured_decision(capture: dict, ops) -> Optional[LeadDecision]:
        raw = capture.get("decision")
        if raw is None:
            raw = getattr(ops, "captured_lead_decision", None)
        if raw is None:
            return None
        if isinstance(raw, LeadDecision):
            return raw
        try:
            return parse_lead_decision(raw)
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
