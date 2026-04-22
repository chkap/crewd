"""crewd CLI entrypoint."""
from __future__ import annotations
from pathlib import Path
import typer
from typing import Optional

from . import commands

app = typer.Typer(
    name="crewd",
    help="Multi-agent coding crew CLI — workspace-based, GitHub Issues as bus.",
    no_args_is_help=True,
    add_completion=False,
)


def _ws_opt(workspace: Optional[Path]) -> Path:
    return workspace or Path.cwd()


@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="Workspace directory to create/init."),
    name: Optional[str] = typer.Option(None, "--name", help="Crew name (default: dir name)."),
    repo: Optional[str] = typer.Option(None, "--repo", help="Target repo owner/name (optional, can attach later)."),
):
    """Initialize a new crew workspace."""
    raise typer.Exit(commands.cmd_init(path, name, repo))


@app.command()
def attach(
    repo: str = typer.Argument(..., help="Target repo owner/name."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
    branch: Optional[str] = typer.Option(None, "--branch"),
    clone: bool = typer.Option(True, "--clone/--no-clone", help="Clone repo into workspace/checkout."),
):
    """Attach a target GitHub repo to the workspace."""
    raise typer.Exit(commands.cmd_attach(_ws_opt(workspace), repo, branch, clone))


@app.command()
def doctor(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w")):
    """Verify workspace, backend, and config sanity."""
    raise typer.Exit(commands.cmd_doctor(_ws_opt(workspace)))


@app.command()
def goal(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
    edit: bool = typer.Option(False, "--edit", "-e", help="Open GOAL.md in $EDITOR."),
):
    """Print or edit GOAL.md."""
    raise typer.Exit(commands.cmd_goal(_ws_opt(workspace), edit))


@app.command()
def run(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
    once: bool = typer.Option(False, "--once", help="Run a single cycle, then exit."),
    role: Optional[str] = typer.Option(None, "--role", help="Tick only this role once, then exit."),
):
    """Run the round-table loop in foreground."""
    raise typer.Exit(commands.cmd_run(_ws_opt(workspace), once, role))


@app.command()
def stop(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
    reason: str = typer.Option("manual", "--reason"),
):
    """Place STOPPED sentinel — running `run` will exit at next check."""
    raise typer.Exit(commands.cmd_stop(_ws_opt(workspace), reason))


@app.command()
def status(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w")):
    """Show workspace status."""
    raise typer.Exit(commands.cmd_status(_ws_opt(workspace)))


@app.command()
def resume(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w")):
    """Clear STOPPED sentinel."""
    raise typer.Exit(commands.cmd_resume(_ws_opt(workspace)))


@app.command()
def logs(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
    role: Optional[str] = typer.Option(None, "--role", "-r"),
    cycle: Optional[int] = typer.Option(None, "--cycle", "-c"),
    tail: int = typer.Option(50, "--tail", "-n"),
    follow: bool = typer.Option(False, "--follow", "-f"),
):
    """List or print role logs."""
    raise typer.Exit(commands.cmd_logs(_ws_opt(workspace), role, cycle, tail, follow))


@app.command(name="list")
def list_(prune: bool = typer.Option(False, "--prune", help="Remove registry entries whose path no longer exists.")):
    """List all registered crewd workspaces."""
    raise typer.Exit(commands.cmd_list(prune))


@app.command()
def cd(name: str = typer.Argument(..., help="Workspace name (exact or unique prefix).")):
    """Print the absolute path of a registered workspace. Use as: cd $(crewd cd <name>)."""
    raise typer.Exit(commands.cmd_cd(name))


@app.command()
def talk(
    role: str = typer.Argument(..., help="Target role: lead | worker | verifier | advisory."),
    message: str = typer.Argument(..., help="Message to queue for the role."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
):
    """Queue an operator message in a role's inbox; consumed on next tick."""
    raise typer.Exit(commands.cmd_talk(_ws_opt(workspace), role, message))


@app.command()
def inbox(
    role: str = typer.Argument(..., help="Target role: lead | worker | verifier | advisory."),
    priority: str = typer.Argument(..., help="Priority: OVERRIDE | ADVICE | INFO."),
    message: str = typer.Argument(..., help="Message body."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
):
    """Append a prefixed line to a role's inbox (consumed on next tick)."""
    raise typer.Exit(commands.cmd_inbox_append(_ws_opt(workspace), role, priority, message))


@app.command()
def tick(
    role: str = typer.Argument(..., help="Role to tick once."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
):
    """Force a single tick for one role, ignoring the loop schedule."""
    raise typer.Exit(commands.cmd_tick(_ws_opt(workspace), role))


if __name__ == "__main__":
    app()
