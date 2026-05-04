"""Tests for ``_install_paths`` — the install-name detection logic.

These tests exercise the fix for the directory-name vs. provider-name
mismatch: hermes-agent's memory-provider discovery keys on the install
directory name (the dir entry under ``<hermes_home>/plugins/``), not the
``name:`` field in ``plugin.yaml``. When the user clones this plugin
under the GitHub repo name ``hermes-agent-forgetful`` (rather than
``forgetful``), setup must write the actual dir name as
``memory.provider`` and register the matching skills path — otherwise
discovery silently fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The plugin lives at this path. Add it to sys.path so we can import
# ``_install_paths`` directly without going through the plugin's
# ``__init__.py`` (which has heavy relative imports).
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PLUGIN_DIR))

from _install_paths import (  # noqa: E402
    install_name_from_path,
    build_skills_dir,
    register_skills_dir_in_config,
)


# ---------------------------------------------------------------------------
# install_name_from_path
# ---------------------------------------------------------------------------

def test_install_name_uses_directory_basename(tmp_path):
    """The install name is the dir entry under ``plugins/``.

    This is the contract hermes-agent's discovery follows — see
    ``plugins/memory/__init__.py:_iter_provider_dirs`` which keys on
    ``child.name``.
    """
    plugin_dir = tmp_path / "plugins" / "hermes-agent-forgetful"
    plugin_dir.mkdir(parents=True)

    assert install_name_from_path(plugin_dir) == "hermes-agent-forgetful"


def test_install_name_canonical_clone(tmp_path):
    """When cloned to the canonical name, that's what gets used."""
    plugin_dir = tmp_path / "plugins" / "forgetful"
    plugin_dir.mkdir(parents=True)

    assert install_name_from_path(plugin_dir) == "forgetful"


def test_install_name_does_not_resolve_symlinks(tmp_path):
    """Use the import path, not the symlink target.

    Hermes-agent loads the plugin via the symlink entry name; the
    provider key it stores is the symlink name. If we resolve, we
    get the underlying directory name instead, which won't match
    what discovery sees.
    """
    real = tmp_path / "src" / "hermes-agent-forgetful"
    real.mkdir(parents=True)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    link = plugins / "forgetful"
    link.symlink_to(real, target_is_directory=True)

    assert install_name_from_path(link) == "forgetful"


# ---------------------------------------------------------------------------
# build_skills_dir
# ---------------------------------------------------------------------------

def test_build_skills_dir_uses_install_name(tmp_path):
    """The skills dir lives under the *install name*, not the manifest name."""
    result = build_skills_dir(str(tmp_path), "hermes-agent-forgetful")

    expected = str(tmp_path.resolve() / "plugins" / "hermes-agent-forgetful" / "skills")
    assert result == expected


def test_build_skills_dir_canonical(tmp_path):
    result = build_skills_dir(str(tmp_path), "forgetful")

    expected = str(tmp_path.resolve() / "plugins" / "forgetful" / "skills")
    assert result == expected


# ---------------------------------------------------------------------------
# register_skills_dir_in_config
# ---------------------------------------------------------------------------

def test_register_skills_dir_creates_section(tmp_path):
    cfg: dict = {}

    register_skills_dir_in_config(cfg, str(tmp_path), "hermes-agent-forgetful")

    expected = str(tmp_path.resolve() / "plugins" / "hermes-agent-forgetful" / "skills")
    assert cfg["skills"]["external_dirs"] == [expected]


def test_register_skills_dir_idempotent(tmp_path):
    """Calling twice is a no-op the second time."""
    cfg: dict = {}

    register_skills_dir_in_config(cfg, str(tmp_path), "forgetful")
    register_skills_dir_in_config(cfg, str(tmp_path), "forgetful")

    assert len(cfg["skills"]["external_dirs"]) == 1


def test_register_skills_dir_preserves_existing_entries(tmp_path):
    cfg = {"skills": {"external_dirs": ["/some/other/dir"]}}

    register_skills_dir_in_config(cfg, str(tmp_path), "forgetful")

    expected = str(tmp_path.resolve() / "plugins" / "forgetful" / "skills")
    assert "/some/other/dir" in cfg["skills"]["external_dirs"]
    assert expected in cfg["skills"]["external_dirs"]


def test_register_skills_dir_repairs_malformed_section(tmp_path):
    """If ``skills`` or ``external_dirs`` are present but the wrong type,
    overwrite with valid structures rather than crashing."""
    cfg = {"skills": "this should be a dict"}

    register_skills_dir_in_config(cfg, str(tmp_path), "forgetful")

    assert isinstance(cfg["skills"], dict)
    assert isinstance(cfg["skills"]["external_dirs"], list)
    assert len(cfg["skills"]["external_dirs"]) == 1
