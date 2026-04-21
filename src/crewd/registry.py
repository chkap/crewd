"""Workspace registry — tracks all crewd workspaces on this machine.

Stored at ~/.config/crewd/registry.json:
  {
    "workspaces": [
      {"name": "demo", "path": "/home/.../demo", "repo": "acme/widget", "registered_at": "..."}
    ]
  }
"""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
import json
import os


def _registry_path() -> Path:
    base = Path(os.environ.get("CREWD_HOME") or (Path.home() / ".config" / "crewd"))
    return base / "registry.json"


def _load() -> dict:
    p = _registry_path()
    if not p.exists():
        return {"workspaces": []}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {"workspaces": []}


def _save(data: dict) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def register(name: str, path: Path, repo: str | None) -> None:
    """Add or update an entry. Idempotent — keyed by absolute path."""
    data = _load()
    abs_path = str(path.resolve())
    data["workspaces"] = [w for w in data["workspaces"] if w["path"] != abs_path]
    data["workspaces"].append({
        "name": name,
        "path": abs_path,
        "repo": repo,
        "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _save(data)


def unregister(path: Path) -> bool:
    data = _load()
    abs_path = str(path.resolve())
    before = len(data["workspaces"])
    data["workspaces"] = [w for w in data["workspaces"] if w["path"] != abs_path]
    if len(data["workspaces"]) == before:
        return False
    _save(data)
    return True


def all_workspaces() -> list[dict]:
    data = _load()
    return list(data["workspaces"])


def find(name: str) -> dict | None:
    """Look up by name (exact match first, then prefix)."""
    entries = all_workspaces()
    exact = [e for e in entries if e["name"] == name]
    if exact:
        return exact[0]
    pref = [e for e in entries if e["name"].startswith(name)]
    if len(pref) == 1:
        return pref[0]
    return None


def prune_missing() -> list[str]:
    """Remove entries whose path no longer exists. Returns removed paths."""
    data = _load()
    keep, removed = [], []
    for w in data["workspaces"]:
        if Path(w["path"]).exists():
            keep.append(w)
        else:
            removed.append(w["path"])
    if removed:
        data["workspaces"] = keep
        _save(data)
    return removed
