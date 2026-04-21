"""Render templates from src/crewd/templates/ into a workspace."""
from __future__ import annotations
from pathlib import Path
from importlib.resources import files
from jinja2 import Environment, FileSystemLoader, StrictUndefined


def _templates_dir() -> Path:
    return Path(str(files("crewd").joinpath("templates")))


def render(name: str, **ctx) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_templates_dir())),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    return env.get_template(name).render(**ctx)


def write_if_absent(path: Path, content: str) -> bool:
    """Write content to path only if it doesn't exist. Returns True if written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True
