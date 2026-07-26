"""Backend protocol — abstracts the underlying coding-agent transport.

crewd runs every role through the official ``github-copilot-sdk`` (programmatic
Copilot **sessions**), driven by the :func:`crewd.session_backend.run_attempt`
lifecycle state machine and dispatched by :mod:`crewd.orchestrator`. The legacy
``copilot -p`` subprocess backend has been retired (issue #17): a workspace
configured with ``backend: copilot`` is rejected at pre-flight with a migration
diagnostic rather than executed.

``SdkBackend`` remains as the thin exit-code adapter for the single-tick
``crewd run --role X`` entry point; the full loop consumes the typed
:class:`~crewd.executor.AttemptExecutor` seam directly.
"""
from __future__ import annotations
from pathlib import Path
from typing import Protocol, runtime_checkable


LEGACY_COPILOT_MIGRATION = (
    "backend: copilot (the `copilot -p` subprocess transport) has been removed. "
    "crewd now runs roles through the official github-copilot-sdk (a required "
    "core dependency). Set `backend: copilot-sdk` in crew.yaml, then run "
    "`crewd doctor`."
)


@runtime_checkable
class Backend(Protocol):
    name: str

    def doctor(self) -> list[str]:
        """Return list of error strings; empty = healthy."""
        ...

    def run_role(
        self,
        role: str,
        model: str,
        config_dir: Path,
        add_dirs: list[Path],
        prompt: str,
        log_path: Path,
        timeout: int,
        cwd: Path,
        first_run: bool,
        *,
        goal_label: str = "",
        workspace_root: Path | None = None,
    ) -> int:
        """Execute one tick for a role. Returns exit code."""
        ...


class SdkBackend:
    """SDK-native single-tick adapter (official ``github-copilot-sdk``).

    Used by the ``crewd run --role X`` entry point, which needs a plain integer
    exit code. It delegates to the typed :class:`~crewd.executor.SdkAttemptExecutor`
    and maps the terminal :class:`~crewd.session_backend.AttemptOutcome` back to a
    legacy exit code. The full run loop does NOT go through here — it drives the
    executor directly via :mod:`crewd.orchestrator` so it can consume the typed
    outcome instead of reconstructing lifecycle meaning from an exit code.

    ``ops_factory`` is injectable so the selectable path is covered by
    deterministic tests without the SDK.
    """

    name = "copilot-sdk"

    # Exit-code mapping at the legacy single-tick edge:
    #   0   clean completion
    #   130 cancelled after wait timeout
    #   124 tainted / unsafe to resume
    #   1   SDK error (incl. pre-execution mounting refusal)
    _EXIT = {
        "idle_completed": 0,
        "aborted_clean": 130,
        "tainted": 124,
        "sdk_error": 1,
    }

    def __init__(self, ops_factory=None):
        self._ops_factory = ops_factory

    def _executor(self):
        from .executor import SdkAttemptExecutor

        return SdkAttemptExecutor(ops_factory=self._ops_factory)

    def doctor(self) -> list[str]:
        return self._executor().doctor()

    def run_role(
        self,
        role: str,
        model: str,
        config_dir: Path,
        add_dirs: list[Path],
        prompt: str,
        log_path: Path,
        timeout: int,
        cwd: Path,
        first_run: bool,
        *,
        goal_label: str = "",
        workspace_root: Path | None = None,
    ) -> int:
        from .executor import AttemptRequest

        ws_root = Path(workspace_root).resolve() if workspace_root is not None else Path(cwd).resolve()
        req = AttemptRequest(
            role=role,
            model=model,
            prompt=prompt,
            config_dir=Path(config_dir),
            add_dirs=[Path(d) for d in add_dirs],
            cwd=Path(cwd),
            workspace_root=ws_root,
            goal_label=goal_label,
            timeout=float(timeout),
            log_path=Path(log_path),
        )
        outcome = self._executor().execute_role(req)
        if outcome.mount_error is not None:
            return 1
        return self._EXIT.get(outcome.result.outcome.value, 1)


def get_backend(name: str) -> Backend:
    if name == "copilot":
        raise ValueError(LEGACY_COPILOT_MIGRATION)
    if name == "copilot-sdk":
        return SdkBackend()
    raise ValueError(f"unknown backend: {name}")
