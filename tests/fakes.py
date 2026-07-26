"""Deterministic fakes for the typed AttemptExecutor seam.

These let the orchestrator + dispatcher be exercised end-to-end with no SDK,
network, or Premium calls. A :class:`FakeExecutor` scripts Lead decisions and
role-attempt outcomes; Lead scripts receive the exact pending-handoff ids the
dispatcher is asking Lead to route, so a decision can acknowledge them.
"""
from __future__ import annotations

import ast
import re
from typing import Callable, Optional

from crewd.dispatcher import LeadDecision
from crewd.executor import (
    AttemptRequest,
    LeadTurnOutcome,
    RoleAttemptOutcome,
    RoleHandoff,
)
from crewd.session_backend import AttemptOutcome, AttemptResult

_IDS_RE = re.compile(r"these handoff ids: (\[[^\]]*\])")


def _pending_ids_from_prompt(prompt: str) -> list[str]:
    m = _IDS_RE.search(prompt)
    if not m:
        return []
    try:
        return list(ast.literal_eval(m.group(1)))
    except Exception:
        return []


def _result(role: str, session_id: str, outcome: AttemptOutcome, error: str | None = None) -> AttemptResult:
    return AttemptResult(
        attempt_id="",
        session_id=session_id,
        role=role,
        outcome=outcome,
        duration=0.01,
        error=error,
        cleanup_confirmed=outcome is not AttemptOutcome.TAINTED,
    )


class FakeExecutor:
    """Scriptable :class:`~crewd.executor.AttemptExecutor`.

    ``lead_script``: an iterable consumed one item per Lead turn. Each item is
    either a :class:`~crewd.dispatcher.LeadDecision`, ``None`` (the Lead turn
    produced no decision), or a callable ``fn(pending_ids: list[str]) -> ...``
    returning one of those. Exhausting the script yields ``None`` (no decision).

    ``role_outcome``: default terminal :class:`AttemptOutcome` for role ticks, or
    a callable ``fn(role, req) -> AttemptOutcome`` / ``fn(role) -> AttemptOutcome``
    for per-role control. ``lead_outcome`` overrides the Lead-turn terminal
    outcome (defaults to IDLE_COMPLETED when a decision is produced, SDK_ERROR
    otherwise).
    """

    def __init__(
        self,
        *,
        lead_script: Optional[list] = None,
        role_outcome=AttemptOutcome.IDLE_COMPLETED,
        lead_outcome: Optional[AttemptOutcome] = None,
        block_until_cancel: bool = False,
        role_handoff=None,
    ):
        self._lead_script = list(lead_script or [])
        self._role_outcome = role_outcome
        self._lead_outcome = lead_outcome
        # Optional structured role handoff (models the submit_role_handoff tool):
        # a :class:`RoleHandoff`, a dict mapping role -> RoleHandoff, or a callable
        # ``fn(role, req) -> RoleHandoff | None``. When it yields a RoleHandoff the
        # role tick reports exactly one submission; None models a role that
        # submitted nothing (a protocol failure on a clean idle).
        self._role_handoff = role_handoff
        # When set, a *role* attempt blocks until the orchestrator trips the
        # CancelToken, then returns CANCELLED_CLEAN — deterministically modelling
        # an in-flight role tick cancelled by an interrupt/operator stop. Lead
        # turns are unaffected (they must still produce the dispatch decision).
        self._block_until_cancel = block_until_cancel
        self.lead_calls: list[AttemptRequest] = []
        self.role_calls: list[AttemptRequest] = []

    def doctor(self) -> list[str]:
        return []

    @staticmethod
    def _await_cancel(cancel) -> None:
        # Deterministic: spin briefly until the poll loop requests cancellation.
        import time

        for _ in range(2000):
            if cancel is not None and cancel.is_requested:
                return
            time.sleep(0.001)

    def execute_role(self, req: AttemptRequest, *, on_started=None, cancel=None) -> RoleAttemptOutcome:
        self.role_calls.append(req)
        sid = f"sess-{req.role}"
        if on_started is not None:
            on_started(sid, 0)
        if self._block_until_cancel:
            self._await_cancel(cancel)
            return RoleAttemptOutcome(
                result=_result(req.role, sid, AttemptOutcome.CANCELLED_CLEAN),
                session_id=sid,
                generation=0,
            )
        outcome = self._resolve_role_outcome(req)
        handoff = self._resolve_role_handoff(req)
        return RoleAttemptOutcome(
            result=_result(req.role, sid, outcome),
            session_id=sid,
            generation=0,
            handoff=handoff,
            handoff_submissions=1 if handoff is not None else 0,
        )

    def run_lead(self, req: AttemptRequest, *, on_started=None, cancel=None) -> LeadTurnOutcome:
        self.lead_calls.append(req)
        pending_ids = _pending_ids_from_prompt(req.prompt)
        item = self._lead_script.pop(0) if self._lead_script else None
        decision = item(pending_ids) if callable(item) else item
        if on_started is not None:
            on_started("sess-lead", 0)
        if self._lead_outcome is not None:
            outcome = self._lead_outcome
        else:
            outcome = (
                AttemptOutcome.IDLE_COMPLETED
                if decision is not None
                else AttemptOutcome.SDK_ERROR
            )
        return LeadTurnOutcome(
            result=_result("lead", "sess-lead", outcome),
            session_id="sess-lead",
            generation=0,
            decision=decision,
        )

    def _resolve_role_outcome(self, req: AttemptRequest) -> AttemptOutcome:
        ro = self._role_outcome
        if callable(ro):
            try:
                return ro(req.role, req)
            except TypeError:
                return ro(req.role)
        if isinstance(ro, dict):
            return ro.get(req.role, AttemptOutcome.IDLE_COMPLETED)
        return ro

    def _resolve_role_handoff(self, req: AttemptRequest):
        rh = self._role_handoff
        if rh is None:
            return None
        if callable(rh):
            try:
                return rh(req.role, req)
            except TypeError:
                return rh(req.role)
        if isinstance(rh, dict):
            return rh.get(req.role)
        return rh


# ── decision-builder helpers for scripts ──
def dispatch_to(role: str):
    """Script item: dispatch to ``role``, acking exactly the pending handoffs."""
    return lambda ids: LeadDecision.dispatch(role, ack=tuple(ids))


def finish(acceptance: str = "done"):
    return lambda ids: LeadDecision.finish(acceptance, ack=tuple(ids))


def wait(condition: str = "external"):
    return lambda ids: LeadDecision.wait(condition, ack=tuple(ids))


def pause(blocker: str = "human-blocked: need input"):
    return lambda ids: LeadDecision.pause(blocker, ack=tuple(ids))


def continue_lead():
    return lambda ids: LeadDecision.continue_lead(ack=tuple(ids))


def role_handoff(
    outcome_class: str = "completed",
    *,
    evidence: str = "",
    changed: str = "",
    remaining: str = "",
    reason: str = "",
    disagreement: str = "",
) -> RoleHandoff:
    """Build a structured role handoff for FakeExecutor's ``role_handoff``."""
    return RoleHandoff(
        outcome_class=outcome_class,
        evidence=evidence,
        changed=changed,
        remaining=remaining,
        reason=reason,
        disagreement=disagreement,
    )
