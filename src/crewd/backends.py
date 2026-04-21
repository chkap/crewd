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
        agent_md: Path,
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
        agent_md: Path,
        add_dirs: list[Path],
        prompt: str,
        log_path: Path,
        timeout: int,
        cwd: Path,
        first_run: bool,
    ) -> int:
        # Build copilot CLI invocation:
        #   copilot --config-dir <cfg> [--continue] --model <m> --add-dir ... --no-color --allow-all-tools -p "<prompt>"
        cmd = [
            "copilot",
            "--config-dir", str(config_dir),
        ]
        if not first_run:
            cmd.append("--continue")
        cmd += ["--model", model, "--no-color", "--allow-all-tools"]
        for d in add_dirs:
            cmd += ["--add-dir", str(d)]
        # Custom-instructions: prepend agent.md as the role prompt header
        agent_text = agent_md.read_text() if agent_md.exists() else ""
        full_prompt = f"{agent_text}\n\n---\n\n{prompt}" if agent_text else prompt
        cmd += ["-p", full_prompt]

        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        with open(log_path, "wb") as logf:
            logf.write(f"$ {' '.join(cmd[:8])} ... (prompt {len(full_prompt)} chars)\n".encode())
            logf.flush()
            try:
                result = subprocess.run(
                    cmd,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    cwd=str(cwd),
                    env=env,
                    timeout=timeout,
                )
                return result.returncode
            except subprocess.TimeoutExpired:
                logf.write(b"\n[crewd] TIMEOUT\n")
                return 124


def get_backend(name: str) -> Backend:
    if name == "copilot":
        return CopilotBackend()
    raise ValueError(f"unknown backend: {name}")
