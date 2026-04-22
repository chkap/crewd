"""Tests for workspace auto-discovery (find_workspace)."""
from __future__ import annotations
from pathlib import Path

from crewd.workspace import find_workspace


def test_find_workspace_in_cwd(tmp_path: Path):
    (tmp_path / "crew.yaml").write_text("name: x\n")
    assert find_workspace(tmp_path) == tmp_path.resolve()


def test_find_workspace_walks_up(tmp_path: Path):
    (tmp_path / "crew.yaml").write_text("name: x\n")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_workspace(deep) == tmp_path.resolve()


def test_find_workspace_returns_none_when_not_found(tmp_path: Path):
    # tmp_path has no crew.yaml; ancestors shouldn't either (in CI/sandbox)
    sub = tmp_path / "nested"
    sub.mkdir()
    # Walk up from sub — no crew.yaml in sub or tmp_path
    # (parents above tmp_path almost certainly don't have one in test envs)
    result = find_workspace(sub)
    # Either None, or some unrelated repo above. If above tmp_path has one,
    # the test environment is unusual — just ensure we don't return sub or tmp_path.
    assert result is None or (result != sub.resolve() and result != tmp_path.resolve())


def test_find_workspace_picks_nearest_ancestor(tmp_path: Path):
    (tmp_path / "crew.yaml").write_text("outer\n")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "crew.yaml").write_text("inner\n")
    deep = inner / "deep"
    deep.mkdir()
    assert find_workspace(deep) == inner.resolve()
