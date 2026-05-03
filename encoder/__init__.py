"""Repo encoder — multi-phase pipeline that populates Forgetful from a codebase.

The encoder spawns a hermes batch session (`hermes -z PROMPT`) with a
self-contained instruction prompt. The agent inside that session uses
hermes' own Read/Glob/Grep tools plus the forgetful_* tools registered
by this plugin to walk the repo and create memories, entities, and
artifacts.

Public surface:
- ``run_encode(target, profile, dry_run)`` — see ``runner.py``.
"""

from .runner import run_encode  # noqa: F401

__all__ = ["run_encode"]
