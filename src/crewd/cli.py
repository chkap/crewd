"""crewd CLI entrypoint."""
from __future__ import annotations
from pathlib import Path
import typer
from typing import Optional

from . import commands
from .workspace import find_workspace

app = typer.Typer(
    name="crewd",
    help="Multi-agent coding crew CLI — workspace-based, GitHub Issues as bus.",
    no_args_is_help=True,
    add_completion=False,
)


def _ws_opt(workspace: Optional[Path]) -> Path:
    """Resolve -w. If None, walk up from cwd looking for crew.yaml. Errors loudly."""
    if workspace is not None:
        return workspace
    found = find_workspace(Path.cwd())
    if found is not None:
        return found
    cwd = Path.cwd().resolve()
    checked = [cwd, *cwd.parents]
    typer.echo("error: no workspace found (no crew.yaml in cwd or any ancestor).", err=True)
    typer.echo("checked:", err=True)
    for c in checked:
        typer.echo(f"  - {c}", err=True)
    typer.echo("\nhint: pass -w/--workspace <path>, or run from inside a workspace, or `crewd init <path>`.", err=True)
    raise typer.Exit(2)


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
    clone: bool = typer.Option(True, "--clone/--no-clone", help="Clone repo into workspace/repo."),
):
    """Attach a target GitHub repo to the workspace."""
    raise typer.Exit(commands.cmd_attach(_ws_opt(workspace), repo, branch, clone))


@app.command()
def doctor(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w")):
    """Print workspace status dashboard with diagnostics."""
    raise typer.Exit(commands.cmd_doctor(_ws_opt(workspace)))


@app.command()
def refresh(workspace: Optional[Path] = typer.Option(None, "--workspace", "-w")):
    """Force re-render agents/*.agent.md from templates + crew.yaml."""
    raise typer.Exit(commands.cmd_refresh(_ws_opt(workspace)))


@app.command()
def goal(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
    edit: bool = typer.Option(False, "--edit", "-e", help="Open GOAL.md in $EDITOR."),
    from_path: Optional[Path] = typer.Option(None, "--from", help="Install GOAL.md from this file."),
):
    """Print or edit GOAL.md."""
    raise typer.Exit(commands.cmd_goal(_ws_opt(workspace), edit, from_path=from_path))


@app.command()
def run(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
    once: bool = typer.Option(False, "--once", help="Run a single cycle, then exit."),
    role: Optional[str] = typer.Option(None, "--role", help="Tick only this role once, then exit."),
    no_auto_render: bool = typer.Option(False, "--no-auto-render", help="Skip auto re-render of agents/ from crew.yaml."),
    daemon: bool = typer.Option(False, "--daemon", "-d", help="Fork the loop into a background daemon process."),
):
    """Run the round-table loop (foreground by default, --daemon for background)."""
    ws = _ws_opt(workspace)
    if daemon:
        if role:
            typer.echo("error: --daemon cannot be used with --role", err=True)
            raise typer.Exit(2)
        raise typer.Exit(commands.cmd_run_daemon(ws, once, auto_render=not no_auto_render))
    raise typer.Exit(commands.cmd_run(ws, once, role, auto_render=not no_auto_render))


@app.command()
def stop(
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
    reason: str = typer.Option("manual", "--reason"),
    force: bool = typer.Option(False, "--force", "-f", help="Send SIGKILL instead of SIGINT to the daemon."),
):
    """Stop the crew: write STOPPED sentinel and signal the daemon if running."""
    raise typer.Exit(commands.cmd_stop(_ws_opt(workspace), reason, force=force))


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
    no_auto_render: bool = typer.Option(False, "--no-auto-render", help="Skip auto re-render of agents/ from crew.yaml."),
):
    """Force a single tick for one role, ignoring the loop schedule."""
    raise typer.Exit(commands.cmd_tick(_ws_opt(workspace), role, auto_render=not no_auto_render))


@app.command(name="new-goal")
def new_goal(
    from_path: Path = typer.Option(Path("./GOAL.md"), "--from", help="Source GOAL.md to install as the new epoch."),
    workspace: Optional[Path] = typer.Option(None, "--workspace", "-w"),
):
    """Start a new goal epoch: bumps version, closes prior labeled issues, resets cycles."""
    raise typer.Exit(commands.cmd_new_goal(_ws_opt(workspace), from_path))


if __name__ == "__main__":
    app()
