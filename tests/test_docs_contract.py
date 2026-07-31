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
DISPATCHER_DOC = (ROOT / "docs" / "dispatcher.md").read_text()
ORCHESTRATOR_DOC = (ROOT / "docs" / "orchestrator.md").read_text()

# Every packaged/operator/architecture surface that describes recovery routing.
# The README cannot be correct while these remain stale, so each is pinned.
_RECOVERY_DOCS = {
    "README.md": README,
    "SKILL.md": SKILL,
    "docs/dispatcher.md": DISPATCHER_DOC,
    "docs/orchestrator.md": ORCHESTRATOR_DOC,
}


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


def test_changelog_0_1_1_entry_documents_recovery_behavior():
    # The 0.1.1 release notes (finalized from the former [Unreleased] section)
    # must accurately cover the #64-#67 behavior on top of 0.1.0.
    assert "## [0.1.1]" in CHANGELOG
    entry = CHANGELOG.split("## [0.1.1]", 1)[1].split("## [0.1.0]", 1)[0]
    assert "continue_lead" in entry  # documents its removal
    assert "WAITING" in entry or "WAIT" in entry
    assert "Advisory" in entry
    # 0.1.1 is a dated, backward-compatible entry with an upgrade path.
    assert "2026-" in entry
    assert "pip install --upgrade crewd" in entry
    # The authoritative version is bumped to 0.1.1 for the release candidate.
    assert '__version__ = "0.1.1"' in (ROOT / "src/crewd/__init__.py").read_text()


# --- cross-document recovery-language contract (Verifier PR #71 audit) -------
#
# The accepted #64-#67 model must be consistent across EVERY packaged/operator/
# architecture surface, not just the README. These deterministic checks pin the
# stale-language regressions the Verifier called out so a fix in one document
# cannot leave another describing a false human PAUSE or a folded budget class.


def test_skill_pause_is_typed_host_recorded_not_lead_written():
    shutdown = SKILL.split("## Graceful shutdown semantics", 1)[1].split("\n## ", 1)[0]
    # Lead no longer hand-writes state/PAUSED; it submits a typed pause decision
    # and the host durably records the human-blocked halt.
    assert "writes `state/PAUSED`" not in shutdown
    assert "Lead writes `state/PAUSED`" not in shutdown
    assert "typed" in shutdown and "pause" in shutdown
    # Transient/uncertain/no-progress are bounded WAITs, not pauses.
    assert "WAITING" in shutdown


def test_dispatcher_doc_thrash_bounds_are_bounded_waiting_not_pause():
    # The per-class thrash bound settles into a bounded WAITING, not a human pause.
    item9 = DISPATCHER_DOC.split("thrash bounds", 1)[1].split("\n10.", 1)[0]
    assert "WAITING" in item9
    assert "NOT a human" in item9 or "not a human" in item9
    # The three recovery classes are named and kept separate.
    for cls in ("transport", "uncertain", "no-progress"):
        assert cls in item9
    # Stale "pauses the run" thrash wording must be gone.
    assert "**pauses** the\n   run" not in DISPATCHER_DOC


def test_dispatcher_doc_invalid_solicitations_settle_into_waiting():
    # Reaching max_invalid_solicitations must settle into WAITING, not persist a
    # human `paused` blocker. (Compare on whitespace-normalized text so the doc's
    # line wrapping doesn't affect the contract.)
    normalized = " ".join(DISPATCHER_DOC.split())
    assert "max_invalid_solicitations` persists a `paused` blocker" not in normalized
    assert (
        "reaching `max_invalid_solicitations` settles the run into a bounded **`WAITING`**"
        in normalized
    )


def test_orchestrator_doc_uncertain_is_own_class_not_no_progress_bound():
    # UNCERTAIN must be documented as its own recovery class with a dedicated
    # budget, not folded into the no-progress thrash bound.
    assert "counts toward the no-progress thrash bound" not in ORCHESTRATOR_DOC
    ctx = ORCHESTRATOR_DOC.split("HandoffOutcome.UNCERTAIN", 1)[1][:400]
    assert "own" in ctx and "budget" in ctx
    assert "separate" in ctx or "rather than being folded" in ctx


def test_no_recovery_surface_advertises_model_selectable_continue_lead():
    # `continue_lead` may appear only as a host-internal/removed concept, never as
    # a model-selectable "keep going" decision. Checked per document: any surface
    # that mentions continue_lead must also carry a host-internal/removed qualifier.
    qualifiers = (
        "host-internal",
        "model-selectable",
        "model-selected",
        "is rejected",
        "removed",
    )
    for name, text in _RECOVERY_DOCS.items():
        normalized = " ".join(text.split()).lower()
        if "continue_lead" not in normalized:
            continue
        # The stale "Lead keeps working itself" framing must never appear.
        assert "continue_lead` — lead keeps working" not in normalized, name
        assert any(q in normalized for q in qualifiers), (
            f"{name} mentions continue_lead without a host-internal/removed qualifier"
        )
