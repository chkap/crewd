"""Installed version-reporting contract for the ``crewd`` CLI (#54).

The public README documents ``crewd --version`` as the way to confirm the
installed version. Prior to #54 the CLI had no such option and rejected it with
``No such option: --version`` (the mismatch flagged in #52). These deterministic
checks lock the documented flag to the single authoritative
``crewd.__version__`` source so docs, CLI, and metadata cannot drift again.

Help-text assertions normalize Rich rendering before matching: in a
color-capable environment (e.g. CI with ``FORCE_COLOR``) Typer styles the option
name so the literal characters of ``--version`` are split by ANSI escape
sequences (``\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-version\\x1b[0m``), and at narrow
widths Rich would otherwise truncate it to ``--versi…``. Stripping ANSI and
rendering at a stable wide width asserts the *semantic* contract — the option is
exposed in help — independently of terminal styling/width. The eager
``crewd --version`` path itself uses plain ``typer.echo`` and is checked
verbatim.
"""
from __future__ import annotations

import re

from typer.testing import CliRunner

from crewd import __version__
from crewd.cli import app

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI SGR escape sequences so styled output matches plainly."""
    return _ANSI.sub("", text)


def test_version_flag_reports_authoritative_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    # The eager version path is plain `typer.echo`; assert it verbatim.
    assert _plain(result.output).strip() == f"crewd {__version__}"


def test_version_flag_does_not_require_a_workspace(tmp_path, monkeypatch):
    # `--version` is eager and must short-circuit before any workspace lookup so
    # it works from any directory, exactly as the README quickstart promises.
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert _plain(result.output).strip() == f"crewd {__version__}"


def test_no_args_still_shows_help():
    # Adding the callback must not change the no-argument help behavior.
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "Usage:" in _plain(result.output)


def test_help_lists_version_option():
    # Render at a stable wide width and strip styling so the assertion reflects
    # the semantic contract, not Rich's color/width-dependent formatting.
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "--version" in _plain(result.output)

