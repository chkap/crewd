"""Tests for daemon mode — PID helpers, stop+signal, status display."""
from __future__ import annotations
from pathlib import Path
import os
import signal

from crewd.workspace import Workspace


def test_pid_file_lifecycle(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_skeleton()
    # Initially no PID
    assert ws.read_pid() is None
    assert not ws.is_daemon_alive()

    # Write and read back
    ws.write_pid(12345)
    assert ws.read_pid() == 12345
    assert ws.pid_file.exists()

    # Clear
    ws.clear_pid()
    assert ws.read_pid() is None
    assert not ws.pid_file.exists()


def test_is_daemon_alive_with_current_pid(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_skeleton()
    # Use our own PID — guaranteed alive
    ws.write_pid(os.getpid())
    assert ws.is_daemon_alive()


def test_is_daemon_alive_with_dead_pid(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_skeleton()
    # PID 2**22 + 7 is almost certainly not running
    ws.write_pid(4194311)
    assert not ws.is_daemon_alive()


def test_daemon_log_path(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    assert ws.daemon_log == tmp_path / "ws" / "state" / "logs" / "daemon.log"


def test_pid_file_bad_content(tmp_path: Path):
    ws = Workspace(tmp_path / "ws")
    ws.ensure_skeleton()
    ws.pid_file.write_text("not-a-number\n")
    assert ws.read_pid() is None
    assert not ws.is_daemon_alive()


def test_cmd_stop_signals_daemon(tmp_path: Path, tmp_ws: Workspace):
    """cmd_stop with a live daemon PID writes STOPPED + sends SIGINT."""
    import crewd.commands as commands

    # Fork a child that just sleeps, so we have a real PID to signal
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        signal.signal(signal.SIGINT, lambda *_: os._exit(0))
        os.write(w, b"1")
        os.close(w)
        import time; time.sleep(30)
        os._exit(1)
    os.close(w)
    os.read(r, 1)  # wait for child to install handler
    os.close(r)

    try:
        tmp_ws.write_pid(pid)
        rc = commands.cmd_stop(tmp_ws.root, "test-stop")
        assert rc == 0
        assert tmp_ws.is_stopped()
        # Child should have exited from SIGINT
        _, status = os.waitpid(pid, os.WNOHANG)
        # Give it a moment
        if status == 0:
            import time; time.sleep(0.5)
            _, status = os.waitpid(pid, os.WNOHANG)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass


def test_cmd_stop_clears_stale_pid(tmp_path: Path, tmp_ws: Workspace):
    """cmd_stop clears stale PID file when process is dead."""
    import crewd.commands as commands

    tmp_ws.write_pid(4194311)  # very likely dead
    rc = commands.cmd_stop(tmp_ws.root, "stale-test")
    assert rc == 0
    assert tmp_ws.is_stopped()
    assert not tmp_ws.pid_file.exists()


def test_cmd_status_shows_daemon(tmp_ws: Workspace, capsys):
    """cmd_status includes daemon info."""
    import crewd.commands as commands

    # No daemon
    commands.cmd_status(tmp_ws.root)

    # With live PID
    tmp_ws.write_pid(os.getpid())
    rc = commands.cmd_status(tmp_ws.root)
    assert rc == 0
