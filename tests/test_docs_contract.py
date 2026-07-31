"""Documentation contract for the PyPI-first packaged-user guidance (#67).

These deterministic string checks pin the user-facing documentation to the
accepted #64-#66 behavior and the primary ``pip install`` + virtual-environment
install path, so the packaged README/SKILL/CHANGELOG cannot silently regress to
stale routing language (e.g. a model-selectable ``continue_lead``) or bury the
supported install flow behind the tool-manager alternatives.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()
SKILL = (ROOT / "SKILL.md").read_text()
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()


def test_readme_makes_pip_venv_install_primary():
    install = README.split("## Installation", 1)[1].split("\n## ", 1)[0]
    # A dedicated virtual environment + plain pip is the documented primary path.
    assert "python -m venv" in install
    assert "pip install crewd" in install
    # pip must be presented before the tool-manager alternatives.
    assert install.index("pip install crewd") < install.index("pipx install crewd")
    assert install.index("pip install crewd") < install.index("uv tool install crewd")
    # Version verification is part of the primary flow.
    assert "crewd --version" in install


def test_readme_documents_0_1_0_to_0_1_1_upgrade():
    assert "0.1.0" in README and "0.1.1" in README
    assert "pip install --upgrade crewd" in README
    assert "crewd refresh" in README and "crewd doctor" in README


def test_readme_has_no_model_selectable_continue_lead():
    intro = README.split("## Prerequisites", 1)[0]
    # The introductory decision list must not advertise a model-selected
    # "keep going" outcome (removed from the contract in #65).
    assert "continue_lead" not in intro


def test_readme_documents_wait_vs_pause_and_recovery_model():
    assert "Routing & recovery model" in README
    # Per-class recovery budgets and the WAIT-not-PAUSE distinction.
    assert "WAITING" in README
    assert "operator-only" in README.lower() or "operator-only" in README
    assert "wake_condition" in README or "wake condition" in README


def test_readme_documents_high_leverage_advisory_policy():
    assert "High-leverage Advisory" in README
    assert "not a fixed rotation" in README
    assert "non-binding" in README


def test_skill_install_line_leads_with_pip():
    what = SKILL.split("## What crewd is", 1)[1].split("\n## ", 1)[0]
    assert "pip install crewd" in what
    assert what.index("pip install crewd") < what.index("pipx install crewd")
    # SKILL routing summary must not advertise a model-selectable continue_lead.
    assert "no model-selected `continue_lead`" in SKILL


def test_changelog_has_unreleased_section_documenting_recovery_behavior():
    assert "## [Unreleased]" in CHANGELOG
    unreleased = CHANGELOG.split("## [Unreleased]", 1)[1].split("## [0.1.0]", 1)[0]
    assert "continue_lead" in unreleased  # documents its removal
    assert "WAITING" in unreleased or "WAIT" in unreleased
    assert "Advisory" in unreleased
    # The authoritative version is not bumped by this documentation change.
    assert '__version__ = "0.1.0"' in (ROOT / "src/crewd/__init__.py").read_text()
