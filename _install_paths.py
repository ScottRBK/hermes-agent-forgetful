"""Install-path resolution for the Forgetful Hermes plugin.

Hermes-agent's memory-provider discovery keys on the directory entry name
under ``<hermes_home>/plugins/`` (see
``hermes_cli/plugins.py:_iter_provider_dirs``). The ``name:`` field in
``plugin.yaml`` is informational only — the dir name is what's written
to ``memory.provider`` and what becomes the ``hermes <name>`` subcommand.

When the user clones this plugin under the GitHub repo name
``hermes-agent-forgetful`` (rather than ``forgetful``), setup must use
that actual dir name everywhere it persists state.

This module is deliberately stdlib-only and has no relative imports so
it can be imported in tests without pulling in the rest of the plugin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


def install_name_from_path(plugin_root: Path) -> str:
    """Return the directory name this plugin is installed under.

    Uses ``Path.name`` (not ``Path.resolve().name``) so symlinked installs
    report the symlink name — which matches what hermes-agent's discovery
    sees when iterating the plugins directory.
    """
    return plugin_root.name


def build_skills_dir(hermes_home: str, install_name: str) -> str:
    """Return the absolute path to this plugin's bundled ``skills/`` dir."""
    return str(Path(hermes_home).resolve() / "plugins" / install_name / "skills")


def register_skills_dir_in_config(
    hermes_config: Dict[str, Any],
    hermes_home: str,
    install_name: str,
) -> None:
    """Append this plugin's ``skills/`` to ``skills.external_dirs`` (idempotent).

    Hermes scans every directory in ``skills.external_dirs`` for SKILL.md
    files at agent startup. Registering our plugin's skills/ here means
    the encode-repo skill (and any future ones we ship) get picked up
    automatically without the user copying files into ``~/.hermes/skills/``.
    """
    plugin_skills_dir = build_skills_dir(hermes_home, install_name)

    skills_section = hermes_config.get("skills")
    if not isinstance(skills_section, dict):
        skills_section = {}
        hermes_config["skills"] = skills_section

    external = skills_section.get("external_dirs")
    if not isinstance(external, list):
        external = []
        skills_section["external_dirs"] = external

    target = str(Path(plugin_skills_dir).resolve())
    for entry in external:
        try:
            if str(Path(str(entry)).expanduser().resolve()) == target:
                return
        except (OSError, ValueError):
            continue
    external.append(plugin_skills_dir)
