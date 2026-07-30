"""Installed version-reporting contract for the ``crewd`` CLI (#54).

The public README documents ``crewd --version`` as the way to confirm the
installed version. Prior to #54 the CLI had no such option and rejected it with
``No such option: --version`` (the mismatch flagged in #52). These deterministic
checks lock the documented flag to the single authoritative
``crewd.__version__`` source so docs, CLI, and metadata cannot drift again.
"""
from __future__ import annotations

from typer.testing import CliRunner

from crewd import __version__
from crewd.cli import app

runner = CliRunner()


def test_version_flag_reports_authoritative_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output
    assert result.output.strip() == f"crewd {__version__}"


def test_version_flag_does_not_require_a_workspace(tmp_path, monkeypatch):
    # `--version` is eager and must short-circuit before any workspace lookup so
    # it works from any directory, exactly as the README quickstart promises.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"crewd {__version__}"


def test_no_args_still_shows_help():
    # Adding the callback must not change the no-argument help behavior.
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "Usage:" in result.output


def test_help_lists_version_option():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--version" in result.output
