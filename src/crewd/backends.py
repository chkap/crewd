"""Backend protocol — abstracts the underlying coding-agent CLI.

Day 1 implementation: CopilotBackend (GitHub Copilot CLI). The protocol is
designed so other backends (Claude Code, Codex, OpenCode) can be added later
without touching loop / tick logic.
"""
from __future__ import annotations
from pathlib import Path
from typing import Protocol, runtime_checkable
import subprocess
import shutil
import os


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
    ) -> int:
        """Execute one tick for a role. Returns exit code."""
        ...


class CopilotBackend:
    name = "copilot"

    def doctor(self) -> list[str]:
        errs = []
        if not shutil.which("copilot"):
            errs.append("`copilot` CLI not found on PATH. Install GitHub Copilot CLI.")
        return errs

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
    ) -> int:
        # Build copilot CLI invocation:
        #   COPILOT_HOME=<cfg> copilot [--continue] --model <m> --add-dir ... --no-color --allow-all-tools -p "<prompt>"
        #
        # The Copilot CLI dropped the `--config-dir` flag; per-role config and
        # session isolation is now achieved via the COPILOT_HOME env var, which
        # overrides the directory where config + session-state are stored
        # (defaults to $HOME/.copilot). Each role points COPILOT_HOME at its own
        # cfg/<role>/ dir, so `--continue` resumes only that role's session.
        cmd = ["copilot"]
        if not first_run:
            cmd.append("--continue")
        cmd += ["--model", model, "--no-color", "--allow-all-tools"]
        for d in add_dirs:
            cmd += ["--add-dir", str(d)]
        cmd += ["-p", prompt]

        log_path.parent.mkdir(parents=True, exist_ok=True)
        config_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["COPILOT_HOME"] = str(config_dir)
        # Graceful timeout escalation: SIGINT (like Ctrl+C, the path copilot's
        # cancel handler is designed for) -> SIGTERM -> SIGKILL. Each gets a
        # grace window to flush events.jsonl and close in-flight tool blocks.
        # Avoids the orphan tool_use corruption that breaks --continue.
        import signal
        grace_int = 20   # copilot's own cancel handler
        grace_term = 10  # fallback if SIGINT ignored
        with open(log_path, "wb") as logf:
            logf.write(f"$ COPILOT_HOME={config_dir} {' '.join(cmd[:7])} ... (prompt {len(prompt)} chars)\n".encode())
            logf.flush()
            proc = subprocess.Popen(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
                env=env,
            )
            try:
                return proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logf.write(f"\n[crewd] TIMEOUT after {timeout}s - sending SIGINT (Ctrl+C, grace {grace_int}s)\n".encode())
                logf.flush()
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=grace_int)
                    logf.write(b"[crewd] exited cleanly after SIGINT\n")
                    return 130  # conventional exit code for SIGINT
                except subprocess.TimeoutExpired:
                    pass
                logf.write(f"[crewd] SIGINT ignored - escalating to SIGTERM (grace {grace_term}s)\n".encode())
                logf.flush()
                proc.terminate()
                try:
                    proc.wait(timeout=grace_term)
                    logf.write(b"[crewd] exited after SIGTERM\n")
                    return 143
                except subprocess.TimeoutExpired:
                    pass
                logf.write(b"[crewd] SIGTERM ignored - escalating to SIGKILL (session MAY corrupt)\n")
                proc.kill()
                proc.wait()
                return 124


class SdkBackend:
    """SDK-native execution boundary (official ``github-copilot-sdk``).

    Runs one role tick through a real Copilot **session** (via the
    :mod:`crewd.sdk_adapter` transport) driven by the
    :func:`crewd.session_backend.run_attempt` lifecycle state machine, and maps
    the typed :class:`~crewd.session_backend.AttemptOutcome` back to the legacy
    integer exit code the tick loop expects. Selecting ``backend: copilot-sdk``
    is therefore operational, not merely importable.

    Scope boundary with #11: this backend makes a *single existing role tick*
    execute through the SDK. Replacing fixed round-table scheduling and consuming
    richer persisted handoffs is #11 — it reads the typed sidecar this backend
    writes (``<log>.attempt.json``) rather than reconstructing lifecycle meaning
    from exit codes.

    ``ops_factory`` is injectable so the full selectable path (not just the
    domain state machine) is covered by deterministic tests without the SDK.
    """

    name = "copilot-sdk"

    # Exit-code mapping at the legacy Backend edge (typed outcome is persisted
    # separately for #11). Chosen to mirror CopilotBackend's conventions:
    #   0   clean completion            (CLI: normal exit)
    #   130 cancelled after wait timeout (CLI: SIGINT after timeout)
    #   124 tainted / unsafe to resume   (CLI: SIGKILL after timeout)
    #   1   SDK error                    (CLI: generic nonzero)
    _EXIT = {
        "idle_completed": 0,
        "aborted_clean": 130,
        "tainted": 124,
        "sdk_error": 1,
    }

    def __init__(self, ops_factory=None, taint_store_path: Path | None = None):
        self._ops_factory = ops_factory
        self._taint_store_path = taint_store_path

    def doctor(self) -> list[str]:
        from .sdk_adapter import sdk_available

        errs = []
        # The default (real) factory needs the SDK; an injected factory does not.
        if self._ops_factory is None and not sdk_available():
            errs.append(
                "`github-copilot-sdk` (import `copilot`) not installed. "
                "Add it (`pip install 'crewd[sdk]'`) to use backend: copilot-sdk."
            )
        return errs

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
    ) -> int:
        import json

        from .session_backend import (
            AttemptConfig,
            TaintStore,
            TaintedSessionError,
            build_session_id,
            run_attempt,
        )

        config_dir.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Session identity is derived from workspace identity (the role's config
        # dir, which encodes workspace + role) — never merely the role.
        session_id = build_session_id(
            workspace_id=str(config_dir.resolve()), goal_label="", role=role
        )

        taint_path = self._taint_store_path or (config_dir / ".crewd-sdk-taint")
        taint_store = TaintStore(taint_path)

        # Resume is decided by *persisted SDK-init state*, not the CLI's
        # session-state dir (`first_run`): a legacy workspace switching to the
        # SDK backend must not assume an SDK session already exists.
        marker = config_dir / ".crewd-sdk-session"
        resume = marker.exists() and marker.read_text().strip() == session_id

        log_lines: list[str] = [
            f"[crewd] backend=copilot-sdk role={role} model={model} "
            f"session={session_id} resume={resume} first_run={first_run}"
        ]

        # If a resumable session is tainted, conservatively recover by starting a
        # FRESH session (create, not resume) — never auto-resume a tainted id.
        if resume and taint_store.is_tainted(session_id):
            log_lines.append(
                f"[crewd] session {session_id} is tainted; starting a fresh "
                f"session instead of resuming (unsafe-resume needs explicit override)"
            )
            taint_store.clear(session_id)
            resume = False

        # extra_add_dirs / multi-dir compatibility: the SDK exposes a single
        # working_directory and no `--add-dir` equivalent. Surface any dirs we
        # cannot mount with an explicit, actionable diagnostic instead of
        # silently dropping them (a safe mapping is not yet proven — see #10 doc).
        try:
            unmapped = [d for d in add_dirs if Path(d).resolve() != Path(cwd).resolve()]
        except Exception:
            unmapped = list(add_dirs)
        if unmapped:
            log_lines.append(
                "[crewd] WARNING: backend copilot-sdk cannot mount extra add-dirs "
                "(no SDK `--add-dir` equivalent). The role can only access its "
                f"working_directory ({cwd}). Unmapped dirs: "
                + ", ".join(str(d) for d in unmapped)
                + ". Until a safe mapping is proven, place required paths under the "
                "working_directory (e.g. via the role worktree) or keep backend: "
                "copilot for cross-dir roles."
            )

        ops = self._make_ops(
            session_id=session_id,
            role=role,
            model=model,
            config_dir=config_dir,
            working_dir=cwd,
        )

        cfg = AttemptConfig(wait_timeout=float(timeout))
        try:
            result = run_attempt(
                ops, taint_store, prompt=prompt, resume=resume, config=cfg
            )
        except TaintedSessionError as e:
            log_lines.append(f"[crewd] {e}")
            self._write_log(log_path, log_lines)
            return self._EXIT["tainted"]

        # Durable, redacted per-attempt trail (lifecycle + SDK event summaries).
        for ev in result.events:
            log_lines.append(ev.to_line())
        for s in result.session_event_summaries:
            log_lines.append(f"  sdk-event: {s}")
        log_lines.append(
            f"[crewd] outcome={result.outcome.value} duration={result.duration:.3f}s "
            f"tainted={result.tainted} cleanup_confirmed={result.cleanup_confirmed}"
        )
        self._write_log(log_path, log_lines)

        # Persist the typed result for #11 (so the dispatcher need not reconstruct
        # lifecycle meaning from the exit code).
        sidecar = log_path.with_suffix(log_path.suffix + ".attempt.json")
        try:
            sidecar.write_text(
                json.dumps(
                    {
                        "attempt_id": result.attempt_id,
                        "session_id": result.session_id,
                        "role": result.role,
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

        # Record SDK-init state so the next tick resumes deliberately (only when
        # the session is still trustworthy).
        if not result.tainted:
            marker.write_text(session_id)
        elif marker.exists():
            marker.unlink()

        return self._EXIT.get(result.outcome.value, 1)

    def _make_ops(self, *, session_id, role, model, config_dir, working_dir):
        if self._ops_factory is not None:
            return self._ops_factory(
                session_id=session_id,
                role=role,
                model=model,
                config_dir=config_dir,
                working_dir=working_dir,
            )
        from .sdk_adapter import SdkRoleRuntime

        return SdkRoleRuntime(
            session_id=session_id,
            role=role,
            model=model,
            config_dir=config_dir,
            working_dir=working_dir,
        )

    @staticmethod
    def _write_log(log_path: Path, lines: list[str]) -> None:
        log_path.write_text("\n".join(lines) + "\n")


def get_backend(name: str) -> Backend:
    if name == "copilot":
        return CopilotBackend()
    if name == "copilot-sdk":
        return SdkBackend()
    raise ValueError(f"unknown backend: {name}")
