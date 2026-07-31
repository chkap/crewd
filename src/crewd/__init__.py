"""crewd — multi-agent coding crew CLI.

``__version__`` below is the single authoritative version source for the whole
project: the distribution version in ``pyproject.toml`` is declared ``dynamic``
and read from this attribute by Hatchling (``[tool.hatch.version]``), so package
metadata and ``crewd.__version__`` cannot drift.

Typed-package policy: crewd is distributed as a command-line application; its
supported public contract is the ``crewd`` CLI, not an importable Python API.
The importable package therefore does not ship a ``py.typed`` marker and makes
no typed-library stability guarantee. Revisit this only if/when a typed public
API is intentionally offered and verified by a type checker in CI.
"""
__version__ = "0.1.1"

