"""Tests for crewd.config — schema, save/load roundtrip, family validation."""
from __future__ import annotations
from pathlib import Path
import pytest

from crewd.config import CrewConfig, RoleConfig, default_config


def test_default_config_has_four_roles():
    cfg = default_config("demo", "acme/widget")
    assert set(cfg.roles) == {"lead", "worker", "verifier", "advisory"}
    assert cfg.target.repo == "acme/widget"
    assert cfg.target.branch == "main"
    assert cfg.backend == "copilot"


def test_family_validation_passes_by_default():
    cfg = default_config("demo")
    assert cfg.validate_families() == []


def test_family_validation_catches_same_family():
    cfg = default_config("demo")
    cfg.roles["verifier"] = RoleConfig(model="gpt-5.4", family="gpt")  # same as worker
    errs = cfg.validate_families()
    assert len(errs) == 1
    assert "rubber stamp" in errs[0]


def test_family_validation_skips_when_role_missing():
    cfg = default_config("demo")
    del cfg.roles["verifier"]
    assert cfg.validate_families() == []


def test_save_load_roundtrip(tmp_path: Path):
    cfg = default_config("demo", "acme/widget")
    cfg.loop.sleep_secs = 30
    p = tmp_path / "crew.yaml"
    cfg.save(p)
    loaded = CrewConfig.load(p)
    assert loaded.name == "demo"
    assert loaded.target.repo == "acme/widget"
    assert loaded.loop.sleep_secs == 30
    assert loaded.roles["worker"].family == "gpt"
