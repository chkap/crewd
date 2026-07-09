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


def get_backend(name: str) -> Backend:
    if name == "copilot":
        return CopilotBackend()
    raise ValueError(f"unknown backend: {name}")
