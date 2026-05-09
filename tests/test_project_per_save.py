"""Tests for the per-save project model — surviving slice after the
2026-05-09 meta-tool rip-out.

Most of the original tests in this file pinned the *curated* save / recall /
projects tools. Those tools were removed when the plugin migrated to
exposing Forgetful's three meta-tools (discover, how_to_use, execute)
directly. The behavior they covered (project name resolution, project_ids
translation, missing-project tool_error) now lives in the agent's prompts:
the LLM calls execute_forgetful_tool with create_memory / list_projects
/ create_project directly and surfaces project ids itself.

What remains in this file is the per-save model behavior the plugin
*still owns*:

- ``system_prompt_block`` lists currently-available projects so the agent
  has ambient awareness when deciding where to file a memory. A cwd-based
  hint marks the project that matches the current git remote, if any.

- ``ForgetfulConfig.project_id`` is gone. There is no "active project"
  sticky state anywhere — every save is an explicit per-call decision.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_HERMES_AGENT = Path(os.environ.get("HERMES_AGENT_DIR", "/home/scott/.hermes/hermes-agent"))

if not (_HERMES_AGENT / "agent" / "memory_provider.py").exists():
    pytest.skip(
        "hermes-agent not available — set HERMES_AGENT_DIR to enable",
        allow_module_level=True,
    )

if str(_HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(_HERMES_AGENT))


@pytest.fixture()
def provider_module():
    """Load the plugin in a fresh package namespace per test."""
    pkg_name = "_test_forgetful_per_save"
    for key in [k for k in sys.modules if k == pkg_name or k.startswith(pkg_name + ".")]:
        del sys.modules[key]

    spec = importlib.util.spec_from_file_location(
        pkg_name,
        str(_PLUGIN_DIR / "__init__.py"),
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _provider_with_projects(provider_module, projects, *, cwd_id=None, mode="hybrid"):
    """Build a provider with the ambient state system_prompt_block needs.

    No tool dispatch happens in these tests — they assert on the static
    prompt body — so the client is stubbed minimally.
    """
    provider = provider_module.ForgetfulMemoryProvider()

    class _StubClient:
        def is_alive(self):
            return True
        def close(self):
            pass

    provider._client = _StubClient()
    provider._projects_cache = list(projects)
    provider._cwd_project_id = cwd_id
    provider._recall_mode = mode
    return provider


# ---------------------------------------------------------------------------
# system_prompt_block ambient project list
# ---------------------------------------------------------------------------

def test_system_prompt_lists_available_projects(provider_module):
    """When projects exist, the system prompt block lists their names+ids
    so the agent has ambient awareness when picking one for a save."""
    projects = [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    provider = _provider_with_projects(provider_module, projects)

    block = provider.system_prompt_block()

    assert "alpha" in block
    assert "beta" in block


def test_system_prompt_marks_cwd_matched_project(provider_module):
    """If the cwd's git remote matches a project, it should be marked in
    the prompt as a hint — not enforced, just a nudge."""
    projects = [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    provider = _provider_with_projects(provider_module, projects, cwd_id=2)

    block = provider.system_prompt_block()

    # The cwd-matched project should carry some kind of marker that the
    # other project does not. Pin behaviour, not exact glyph.
    alpha_pos = block.find("alpha")
    beta_pos = block.find("beta")
    assert alpha_pos >= 0 and beta_pos >= 0
    beta_line = next(
        line for line in block.splitlines() if "beta" in line
    )
    alpha_line = next(
        line for line in block.splitlines() if "alpha" in line
    )
    hint_terms = ("cwd", "current", "match", "←")
    assert any(t in beta_line.lower() or t in beta_line for t in hint_terms), (
        f"beta line should carry a cwd-hint marker; got: {beta_line!r}"
    )
    assert not any(
        t in alpha_line.lower() or t in alpha_line for t in hint_terms
    ), f"alpha line should NOT carry a hint; got: {alpha_line!r}"


# ---------------------------------------------------------------------------
# Config: project_id field is gone
# ---------------------------------------------------------------------------

def test_config_no_longer_has_project_id_field(provider_module):
    """``ForgetfulConfig`` must not declare a ``project_id`` field — there
    is no such thing as 'the active project' anymore. Setting one in
    forgetful.json should be silently ignored, not loaded into state."""
    from dataclasses import fields
    cfg_cls = provider_module.ForgetfulConfig
    field_names = {f.name for f in fields(cfg_cls)}
    assert "project_id" not in field_names
    assert "project_name" not in field_names
