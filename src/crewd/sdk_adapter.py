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
        self._loop: _LoopThread | None = None
        self._client = None  # copilot.CopilotClient
        self._session = None  # copilot.CopilotSession
        self._event_count = 0

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

    def abort(self, timeout: float) -> bool:
        assert self._session is not None and self._loop is not None
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
