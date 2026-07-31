"""Concrete Copilot SDK adapter behind the :mod:`crewd.session_backend` port.

Implements :class:`crewd.session_backend.SdkOps` using the official
``github-copilot-sdk`` (``copilot``) package. This is the *only* module that
imports the SDK, so the rest of crewd — and all unit tests — depend on the
narrow domain port instead of SDK internals.

Transport decision (see ``docs/sdk-backend.md``): **one SDK-owned child (stdio)
runtime per role** (Advisory option 1). Each role gets an independent
``CopilotClient`` with its own ``config_directory`` and ``working_directory``,
giving each role a separate failure/shutdown domain that mirrors the existing
per-role config/worktree isolation. No TCP port is exposed.

The SDK API is ``asyncio``-based; crewd's loop is synchronous, so this adapter
owns a single background event loop thread and marshals every SDK call onto it.

.. note::
   This module is import-guarded: importing it does not require the SDK to be
   installed. The SDK is imported lazily inside :meth:`SdkRoleRuntime.open` so
   the domain port, state machine, and their tests never depend on the package.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from .session_backend import RunSignal, SdkError, SdkOps


class _LoopThread:
    """A dedicated asyncio loop running on its own thread for sync bridging."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="crewd-sdk-loop")
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro, timeout: float | None = None):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def schedule(self, coro) -> None:
        """Fire-and-forget: run ``coro`` on the loop without blocking the caller.

        Used for a non-blocking abort request from another thread (the control
        poll) so it never waits on the SDK.
        """
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        try:
            self._loop.close()
        except Exception:
            pass


class SdkRoleRuntime(SdkOps):
    """One role's SDK runtime + session, implementing the :class:`SdkOps` port.

    Owns exactly one ``CopilotClient`` (SDK-owned child stdio runtime) and one
    persistent named session. Constructed per attempt by the backend; a fresh
    instance is cheap and keeps failure domains isolated per role.
    """

    def __init__(
        self,
        *,
        session_id: str,
        role: str,
        model: str,
        config_dir: Path,
        working_dir: Path,
        available_tools: list[str] | None = None,
        excluded_tools: list[str] | None = None,
        reasoning_effort: str | None = None,
        allow_all_tools: bool = True,
        lead_decision_capture: dict | None = None,
        role_handoff_capture: dict | None = None,
    ) -> None:
        self.session_id = session_id
        self.role = role
        self.model = model
        self.config_dir = config_dir
        self.working_dir = working_dir
        self.available_tools = available_tools
        self.excluded_tools = excluded_tools
        self.reasoning_effort = reasoning_effort
        self.allow_all_tools = allow_all_tools
        # When set (a Lead decision turn), the ``submit_lead_decision`` custom
        # tool records its single candidate into this attempt-local
        # :class:`~crewd.executor.LeadDecisionCapture` — memory only. The handler
        # must NOT mutate any durable dispatcher state; the one consuming
        # transaction is resolve_lead_solicitation.
        self._lead_capture = lead_decision_capture
        # When set (a non-Lead dispatched tick), the ``submit_role_handoff``
        # custom tool records its single structured outcome into this
        # attempt-local :class:`~crewd.executor.RoleHandoffCapture` — memory
        # only. Same rule as the Lead capture: the handler never mutates durable
        # dispatcher state; the one consuming transaction is record_terminal.
        self._role_handoff_capture = role_handoff_capture
        self._loop: _LoopThread | None = None
        self._client = None  # copilot.CopilotClient
        self._session = None  # copilot.CopilotSession
        self._event_count = 0
        # Guards single-issue of the SDK abort so an external cancel request and
        # the state machine's confirming abort never double-escalate the runtime.
        self._abort_lock = threading.Lock()
        self._abort_issued = False

    # ── SdkOps ──
    def open(self, *, resume: bool) -> None:
        try:
            import copilot  # lazy: only needed for the real backend
        except Exception as e:  # pragma: no cover - environment dependent
            raise SdkError(f"github-copilot-sdk not importable: {e}") from e

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._loop = _LoopThread()
        try:
            self._client = copilot.CopilotClient(working_directory=str(self.working_dir))
            self._loop.run(self._client.start())
            kwargs = dict(
                model=self.model,
                session_id=self.session_id,
                working_directory=str(self.working_dir),
                config_directory=str(self.config_dir),
                on_permission_request=_permission_handler(copilot, self.allow_all_tools),
            )
            if self.available_tools is not None:
                kwargs["available_tools"] = self.available_tools
            if self.excluded_tools is not None:
                kwargs["excluded_tools"] = self.excluded_tools
            if self.reasoning_effort is not None:
                kwargs["reasoning_effort"] = self.reasoning_effort
            if self._lead_capture is not None:
                self._add_lead_decision_tool(copilot, kwargs)
            if self._role_handoff_capture is not None:
                self._add_role_handoff_tool(copilot, kwargs)
            if resume:
                self._session = self._loop.run(
                    self._client.resume_session(self.session_id, **_drop(kwargs, "session_id"))
                )
            else:
                self._session = self._loop.run(self._client.create_session(**kwargs))
        except SdkError:
            raise
        except Exception as e:
            self._teardown_loop()
            raise SdkError(f"open failed: {e}") from e

    def run(self, prompt: str, timeout: float) -> RunSignal:
        assert self._session is not None and self._loop is not None
        try:
            # Idle oracle = the call returning WITHOUT raising. Per the official
            # SDK, send_and_wait waits for `session.idle` and returns the last
            # assistant-message event, which may legitimately be ``None`` when
            # idle was reached with no assistant message. A wait *timeout* is
            # signalled by TimeoutError, NOT by a None return — so None is IDLE,
            # not a timeout. The in-flight work is never aborted by this call.
            self._loop.run(
                self._session.send_and_wait(prompt, timeout=timeout),
                timeout=timeout + 30,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return RunSignal.WAIT_TIMEOUT
        except Exception as e:
            raise SdkError(f"run failed: {e}") from e
        return RunSignal.IDLE

    def request_abort(self) -> None:
        """Non-blocking best-effort abort request (called from another thread).

        Schedules ``session.abort()`` on the loop *without waiting*, so a blocked
        :meth:`run` (an in-flight ``send_and_wait`` awaiting idle) unblocks. The
        state machine's subsequent :meth:`abort` owns confirmation/escalation;
        this only pokes the runtime once (idempotent via ``_abort_issued``).
        """
        if self._session is None or self._loop is None:
            return
        with self._abort_lock:
            if self._abort_issued:
                return
            self._abort_issued = True
        try:
            # Fire-and-forget: schedule on the loop thread, do not block here.
            self._loop.schedule(self._session.abort())
        except Exception:
            pass

    def abort(self, timeout: float) -> bool:
        assert self._session is not None and self._loop is not None
        # Single abort owner: if a non-blocking request already scheduled the
        # abort, don't issue a second one — just confirm. Otherwise issue it now.
        with self._abort_lock:
            need_issue = not self._abort_issued
            self._abort_issued = True
        if need_issue:
            try:
                self._loop.run(self._session.abort(), timeout=timeout)
            except Exception as e:
                raise SdkError(f"abort failed: {e}") from e
        # Confirm cancellation WITHOUT sending a new turn (an empty
        # send_and_wait would start a fresh turn, neither a read-only idle probe
        # nor proof the aborted turn settled). Instead, poll the durable event
        # history for the SDK's `abort` marker within the bound. A real-time
        # pre-registered idle latch (Advisory option 1) is stronger and is the
        # documented next step once the live smoke pins the exact event contract.
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            try:
                events = self._loop.run(self._session.get_events(), timeout=10)
            except Exception:
                return False
            if any(_is_abort_or_idle_event(ev) for ev in events[self._event_count:]):
                return True
            _time.sleep(0.2)
        return False

    def drain_events(self) -> list[str]:
        if self._session is None or self._loop is None:
            return []
        try:
            events = self._loop.run(self._session.get_events(), timeout=30)
        except Exception:
            return []
        summaries = []
        for ev in events[self._event_count:]:
            summaries.append(_summarise_event(ev))
        self._event_count = len(events)
        return summaries

    def disconnect(self) -> None:
        try:
            if self._session is not None and self._loop is not None:
                self._loop.run(self._session.disconnect(), timeout=30)
            if self._client is not None and self._loop is not None:
                self._loop.run(self._client.stop(), timeout=30)
        except Exception as e:
            raise SdkError(f"disconnect failed: {e}") from e
        finally:
            self._teardown_loop()

    def force_stop(self) -> None:
        try:
            if self._client is not None and self._loop is not None:
                self._loop.run(self._client.force_stop(), timeout=30)
        except Exception as e:
            raise SdkError(f"force_stop failed: {e}") from e
        finally:
            self._teardown_loop()

    # ── helpers ──
    def _add_lead_decision_tool(self, copilot_mod, kwargs: dict) -> None:
        """Register the ``submit_lead_decision`` custom tool for a Lead turn.

        Built through the official SDK ``define_tool`` API and passed via the
        ``tools=[...]`` create_session parameter. The handler ONLY records the
        (untrusted) candidate payload into the attempt-local capture — it never
        touches durable dispatcher state; the single consuming transaction is
        :meth:`crewd.dispatcher.Dispatcher.resolve_lead_solicitation`.

        Registration failure is **not** swallowed: it propagates to
        :meth:`open`, which surfaces it as an :class:`SdkError` (a failed Lead
        turn), so a signature drift can never silently drop the only
        decision-delivery channel and let the run masquerade as healthy.
        """
        if self._lead_capture is None:
            return
        tool = make_lead_decision_tool(copilot_mod, self._lead_capture)
        kwargs.setdefault("tools", []).append(tool)

    def _add_role_handoff_tool(self, copilot_mod, kwargs: dict) -> None:
        """Register the ``submit_role_handoff`` custom tool for a dispatched role.

        Parallel to :meth:`_add_lead_decision_tool`: built through the official
        ``define_tool`` API and passed via ``tools=[...]``. The handler ONLY
        records the (untrusted) structured outcome into the attempt-local capture
        — it never touches durable dispatcher state; the single consuming
        transaction is :meth:`crewd.dispatcher.Dispatcher.record_terminal`.

        Registration failure is **not** swallowed: it propagates to :meth:`open`
        as an :class:`SdkError`, so a signature drift can never silently drop the
        role's only structured-return channel and let a hollow completion pass.
        """
        if self._role_handoff_capture is None:
            return
        tool = make_role_handoff_tool(copilot_mod, self._role_handoff_capture)
        kwargs.setdefault("tools", []).append(tool)

    def _teardown_loop(self) -> None:
        if self._loop is not None:
            self._loop.close()
            self._loop = None


def _drop(d: dict, key: str) -> dict:
    return {k: v for k, v in d.items() if k != key}


def _permission_handler(copilot_mod, allow_all: bool):
    """Return an SDK-shaped permission handler ``(request, invocation) -> result``.

    The official SDK invokes the handler as ``handler(permission_request,
    {"session_id": ...})`` and expects a typed ``PermissionRequestResult`` — NOT
    a ``{"result": ...}`` dict. Getting this wrong means a prompt-only smoke may
    pass while the first permissioned shell/file op silently fails to preserve
    legacy ``--allow-all-tools`` behaviour (Advisory).

    Allow-all reuses the SDK's own ``PermissionHandler.approve_all`` (returns
    ``PermissionDecisionApproveOnce``), matching the legacy default. Deny returns
    a typed ``PermissionDecisionUserNotAvailable`` — the same decision the SDK
    itself falls back to when no handler can satisfy a request.
    """
    if allow_all:
        # Official approve_all is exactly `(request, invocation) -> approve-once`.
        return copilot_mod.PermissionHandler.approve_all

    from copilot.generated.rpc import PermissionDecisionUserNotAvailable

    def _deny(_request, _invocation):
        return PermissionDecisionUserNotAvailable()

    return _deny


def _is_abort_or_idle_event(ev) -> bool:
    """True if a durable SDK event marks the aborted turn as settled.

    Matches the SDK's `abort` history event (and idle markers) by type name,
    without depending on a concrete event class. The exact type is pinned by the
    live smoke; this is deliberately tolerant of naming.
    """
    t = getattr(ev, "type", None)
    name = str(getattr(t, "value", t) or "").lower()
    return "abort" in name or "idle" in name or "cancel" in name


def _summarise_event(ev) -> str:
    """Compact, non-raw summary of one SDK session event."""
    t = getattr(ev, "type", None)
    tname = getattr(t, "value", str(t))
    return f"event:{tname}"


def sdk_available() -> bool:
    """True if the official SDK is importable (used by the doctor check)."""
    try:
        import copilot  # noqa: F401
        return True
    except Exception:
        return False


# ── submit_lead_decision custom tool (official define_tool API) ──
_LEAD_TOOL_DESCRIPTION = (
    "Submit EXACTLY ONE routing decision for the crew this turn. Call this tool "
    "once and only once. Fields: kind (one of dispatch, wait, "
    "pause, finish); ack_handoff_ids (the list of pending handoff ids this "
    "decision acknowledges); role (target role, required for kind=dispatch); "
    "task_number (the exact crewd:task issue number this dispatch targets — "
    "REQUIRED for kind=dispatch to worker/verifier so the routed task identity "
    "is carried through the attempt, handoff, and Verifier routing rather than "
    "re-guessed from the public record); intent (the mode this dispatch is "
    "routed under: one of implementation, verifier_audit, acceptance, release, "
    "advisory — defaults to implementation; use verifier_audit/acceptance/"
    "release for a Lead-assigned verifier-only task that has no Worker PR or "
    "readiness to review); reason (optional rationale); "
    "wake_condition (required for kind=wait); human_blocker (required for "
    "kind=pause — reserve pause for a genuine operator-only prerequisite such as "
    "a missing credential, authorization, protected-environment approval, or "
    "product/policy decision, NOT for internal retries); final_acceptance "
    "(required for kind=finish). If you need more analysis before routing, choose "
    "wait with an observable wake_condition rather than looping — the host "
    "re-solicits you under a bounded budget."
)


def _lead_decision_params_model():
    """Build the Pydantic params model for the submit_lead_decision tool.

    Imported lazily so this module stays import-safe without the SDK/pydantic.
    """
    from typing import List, Optional

    from pydantic import BaseModel, Field

    class SubmitLeadDecisionParams(BaseModel):
        kind: str = Field(
            description="one of: dispatch, wait, pause, finish"
        )
        ack_handoff_ids: List[str] = Field(
            default_factory=list,
            description="pending handoff ids this decision acknowledges",
        )
        role: Optional[str] = Field(
            default=None, description="target role (required for kind=dispatch)"
        )
        task_number: Optional[int] = Field(
            default=None,
            description=(
                "exact crewd:task issue number this dispatch targets "
                "(required for kind=dispatch to worker/verifier)"
            ),
        )
        intent: Optional[str] = Field(
            default=None,
            description=(
                "dispatch mode: implementation (default), verifier_audit, "
                "acceptance, release, or advisory; use a verifier-only intent "
                "for a Lead-assigned audit/acceptance with no Worker PR"
            ),
        )
        reason: Optional[str] = Field(default=None, description="optional rationale")
        wake_condition: Optional[str] = Field(
            default=None, description="required for kind=wait"
        )
        human_blocker: Optional[str] = Field(
            default=None, description="required for kind=pause"
        )
        final_acceptance: Optional[str] = Field(
            default=None, description="required for kind=finish"
        )

    return SubmitLeadDecisionParams


def make_lead_decision_handler(capture):
    """Return the inner ``(params, invocation) -> result`` handler.

    Records the decision payload into ``capture`` (a
    :class:`~crewd.executor.LeadDecisionCapture`) enforcing exactly-one
    submission; never mutates durable dispatcher state. A second call is
    rejected with a typed error while the capture still records that multiple
    calls occurred, so a double-submit resolves as an invalid solicitation.
    """

    def _handler(params, invocation=None):
        if hasattr(params, "model_dump"):
            payload = params.model_dump()
        elif isinstance(params, dict):
            payload = dict(params)
        else:  # pragma: no cover - defensive; SDK passes a pydantic model
            payload = {
                k: getattr(params, k)
                for k in dir(params)
                if not k.startswith("_") and not callable(getattr(params, k))
            }
        accepted = capture.submit(payload)
        if not accepted:
            return {
                "accepted": False,
                "error": "exactly one submit_lead_decision call is allowed per turn",
            }
        return {"accepted": True}

    return _handler


def make_lead_decision_tool(copilot_mod, capture):
    """Build the ``submit_lead_decision`` ``Tool`` via the official SDK API.

    Uses ``copilot.define_tool(name, description=, handler=, params_type=)`` —
    the documented tool-definition surface — so an SDK signature drift (e.g. the
    removed ``CustomTool``) fails loudly here instead of silently dropping the
    decision channel.
    """
    handler = make_lead_decision_handler(capture)
    params_type = _lead_decision_params_model()
    return copilot_mod.define_tool(
        "submit_lead_decision",
        description=_LEAD_TOOL_DESCRIPTION,
        handler=handler,
        params_type=params_type,
    )


# ── submit_role_handoff custom tool (official define_tool API) ──
_ROLE_HANDOFF_DESCRIPTION = (
    "Return your structured outcome to Lead for THIS dispatched attempt. Call "
    "this tool EXACTLY ONCE before you finish. Fields: outcome_class (one of "
    "'completed' = you made meaningful progress, or 'no_progress' = nothing "
    "meaningful changed this attempt); evidence (concrete references — PR/issue "
    "numbers, commit shas, test output, logs — REQUIRED for a 'completed' "
    "claim); changed (what state you changed, or 'none' with a short account of "
    "the verifiable outcome you produced without mutation — REQUIRED for a "
    "'completed' claim); remaining (what is left / the suggested next step); "
    "reason (why you are returning control to Lead now — REQUIRED for a "
    "'no_progress' claim); disagreement (optional — any concrete disagreement "
    "with a peer/Lead, reported as evidence, not a routing decision); blocker "
    "(optional — any concrete blocker preventing progress, e.g. a missing "
    "requirement or a human-only decision). disagreement and blocker are "
    "evidence for Lead; they never route. Routing stays with Lead; the transport "
    "lifecycle overrides any success claim if the turn errored, timed out, or "
    "was cancelled."
)


def _role_handoff_params_model():
    """Build the Pydantic params model for the submit_role_handoff tool."""
    from typing import Optional

    from pydantic import BaseModel, Field

    class SubmitRoleHandoffParams(BaseModel):
        outcome_class: str = Field(
            description="one of: completed, no_progress"
        )
        evidence: Optional[str] = Field(
            default=None,
            description="concrete references (PRs, commits, test output); required for 'completed'",
        )
        changed: Optional[str] = Field(
            default=None,
            description="what state changed, or 'none' + verifiable outcome; required for 'completed'",
        )
        remaining: Optional[str] = Field(
            default=None, description="remaining work / suggested next step"
        )
        reason: Optional[str] = Field(
            default=None,
            description="why control is returning to Lead now; required for 'no_progress'",
        )
        disagreement: Optional[str] = Field(
            default=None, description="optional concrete disagreement (evidence, not routing)"
        )
        blocker: Optional[str] = Field(
            default=None, description="optional concrete blocker (evidence, not routing)"
        )

    return SubmitRoleHandoffParams


def make_role_handoff_handler(capture):
    """Return the inner ``(params, invocation) -> result`` handler.

    Records the structured outcome into ``capture`` (a
    :class:`~crewd.executor.RoleHandoffCapture`) enforcing exactly-one
    submission; never mutates durable dispatcher state. A second call is rejected
    with a typed error while the capture still records that multiple calls
    occurred, so a double-submit resolves as a protocol failure (uncertain).
    """

    def _handler(params, invocation=None):
        if hasattr(params, "model_dump"):
            payload = params.model_dump()
        elif isinstance(params, dict):
            payload = dict(params)
        else:  # pragma: no cover - defensive; SDK passes a pydantic model
            payload = {
                k: getattr(params, k)
                for k in dir(params)
                if not k.startswith("_") and not callable(getattr(params, k))
            }
        accepted = capture.submit(payload)
        if not accepted:
            return {
                "accepted": False,
                "error": "exactly one submit_role_handoff call is allowed per attempt",
            }
        return {"accepted": True}

    return _handler


def make_role_handoff_tool(copilot_mod, capture):
    """Build the ``submit_role_handoff`` ``Tool`` via the official SDK API.

    Uses ``copilot.define_tool(...)`` — the same documented surface as
    ``submit_lead_decision`` — so an SDK signature drift fails loudly here
    instead of silently dropping the role's structured-return channel.
    """
    handler = make_role_handoff_handler(capture)
    params_type = _role_handoff_params_model()
    return copilot_mod.define_tool(
        "submit_role_handoff",
        description=_ROLE_HANDOFF_DESCRIPTION,
        handler=handler,
        params_type=params_type,
    )
