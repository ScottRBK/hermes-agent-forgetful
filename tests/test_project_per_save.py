"""Tests for the per-save project model.

Replaces the old "current project" sticky-config model. The contract:

- ``forgetful_save`` accepts an optional ``project`` field (name or id).
  When omitted, the memory is saved global (no ``project_ids``). When a
  name is given, the plugin looks up the id via ``list_projects``. When
  an int is given, it is used directly. Unknown names → ``tool_error``.

- ``forgetful_recall`` accepts an optional ``project_ids`` array. When
  omitted, recall is cross-project (no filter). The old ``scope`` field
  is gone — callers pass ids directly when they want filtering.

- A new ``forgetful_projects`` tool exposes list / create so the agent
  can manage projects without falling back to the meta-tool escape hatch.

- ``system_prompt_block`` lists currently-available projects so the agent
  has ambient awareness when deciding where to file a memory. A cwd-based
  hint marks the project that matches the current git remote, if any.

- ``ForgetfulConfig.project_id`` is gone. There is no "active project"
  sticky state anywhere — every save is an explicit per-call decision.
"""

from __future__ import annotations

import importlib.util
import json
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


def _make_active_provider(provider_module, monkeypatch, *, projects=None):
    """Build a provider with a stubbed client + capturing _execute.

    Returns ``(provider, calls)`` — calls is a list of ``(tool_name, args)``
    tuples in the order they were invoked. The fake _execute returns:
      - ``list_projects`` → {"projects": projects or []}
      - ``create_project`` → {"project": {"id": 999, "name": <name>}}
      - ``create_memory`` → {"id": 12345}
      - ``query_memory`` → {"primary_memories": [], "linked_memories": []}
    """
    provider = provider_module.ForgetfulMemoryProvider()

    class _StubClient:
        def is_alive(self):
            return True

        def close(self):
            pass

    provider._client = _StubClient()
    calls: list[tuple[str, dict]] = []
    projects = list(projects or [])

    def _fake_execute(tool_name, args):
        calls.append((tool_name, dict(args)))
        if tool_name == "list_projects":
            return {"projects": list(projects)}
        if tool_name == "create_project":
            new = {"id": 999, "name": args.get("name"), "description": args.get("description")}
            projects.append(new)
            return {"project": new}
        if tool_name == "create_memory":
            return {"id": 12345}
        if tool_name == "query_memory":
            return {"primary_memories": [], "linked_memories": []}
        return {}

    monkeypatch.setattr(provider, "_execute", _fake_execute)
    return provider, calls


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------

def test_save_schema_has_optional_project_no_scope(provider_module):
    """forgetful_save: ``scope`` is gone, ``project`` is optional and accepts
    either a name (string) or an id (integer)."""
    schema = provider_module.SAVE_SCHEMA
    props = schema["parameters"]["properties"]

    assert "scope" not in props, "the old `scope` field must be removed"
    assert "project" in props, "schema must expose an optional `project` field"
    # Either string-or-integer (oneOf/anyOf) or a permissive type — pin the
    # behavioural contract: not required, accepts both shapes.
    assert "project" not in schema["parameters"].get("required", [])


def test_recall_schema_has_optional_project_ids_no_scope(provider_module):
    """forgetful_recall: ``scope`` is gone, ``project_ids`` is optional and
    is an array of integers."""
    schema = provider_module.RECALL_SCHEMA
    props = schema["parameters"]["properties"]

    assert "scope" not in props, "the old `scope` field must be removed"
    assert "project_ids" in props
    assert props["project_ids"]["type"] == "array"
    assert "project_ids" not in schema["parameters"].get("required", [])


def test_projects_tool_advertised_in_full_surface(provider_module):
    """A new ``forgetful_projects`` tool must appear in the schema list so
    the agent can list / create projects without escape hatches."""
    provider = provider_module.ForgetfulMemoryProvider()
    names = {s["name"] for s in provider.get_tool_schemas()}
    assert "forgetful_projects" in names


# ---------------------------------------------------------------------------
# _handle_save behaviour
# ---------------------------------------------------------------------------

def test_save_without_project_omits_project_ids(provider_module, monkeypatch):
    """No ``project`` arg → memory saved global (no project_ids in payload)."""
    provider, calls = _make_active_provider(provider_module, monkeypatch)

    args = {"title": "T", "content": "body", "context": "why"}
    provider.handle_tool_call("forgetful_save", args)

    create_calls = [c for c in calls if c[0] == "create_memory"]
    assert len(create_calls) == 1
    assert "project_ids" not in create_calls[0][1], (
        "Without a project arg, save must NOT add a project_ids filter"
    )


def test_save_with_int_project_uses_id_directly(provider_module, monkeypatch):
    """``project=42`` → ``project_ids=[42]`` with no list_projects lookup."""
    provider, calls = _make_active_provider(provider_module, monkeypatch)

    args = {"title": "T", "content": "body", "context": "why", "project": 42}
    provider.handle_tool_call("forgetful_save", args)

    assert not any(c[0] == "list_projects" for c in calls), (
        "Integer project ids should not trigger a list_projects lookup"
    )
    create_calls = [c for c in calls if c[0] == "create_memory"]
    assert create_calls[0][1].get("project_ids") == [42]


def test_save_with_string_project_resolves_via_list_projects(provider_module, monkeypatch):
    """``project='myproj'`` → list_projects lookup → project_ids=[id]."""
    projects = [
        {"id": 7, "name": "other"},
        {"id": 42, "name": "myproj"},
    ]
    provider, calls = _make_active_provider(
        provider_module, monkeypatch, projects=projects
    )

    args = {"title": "T", "content": "body", "context": "why", "project": "myproj"}
    provider.handle_tool_call("forgetful_save", args)

    assert any(c[0] == "list_projects" for c in calls)
    create_calls = [c for c in calls if c[0] == "create_memory"]
    assert create_calls[0][1].get("project_ids") == [42]


def test_save_with_unknown_project_returns_tool_error(provider_module, monkeypatch):
    """Unknown project name → structured tool_error, no create_memory call."""
    provider, calls = _make_active_provider(
        provider_module, monkeypatch, projects=[{"id": 1, "name": "exists"}]
    )

    args = {"title": "T", "content": "body", "context": "why", "project": "nope"}
    result = provider.handle_tool_call("forgetful_save", args)

    payload = json.loads(result)
    assert isinstance(payload, dict)
    flat = json.dumps(payload).lower()
    assert "error" in flat
    assert not any(c[0] == "create_memory" for c in calls), (
        "Save must abort when the project name can't be resolved"
    )


# ---------------------------------------------------------------------------
# _handle_recall behaviour
# ---------------------------------------------------------------------------

def test_recall_without_project_ids_is_cross_project(provider_module, monkeypatch):
    """No project_ids arg → cross-project (no filter in payload)."""
    provider, calls = _make_active_provider(provider_module, monkeypatch)

    provider.handle_tool_call("forgetful_recall", {"query": "anything"})

    query_calls = [c for c in calls if c[0] == "query_memory"]
    assert "project_ids" not in query_calls[0][1]
    assert "strict_project_filter" not in query_calls[0][1]


def test_recall_with_project_ids_applies_filter(provider_module, monkeypatch):
    """Explicit project_ids → filter passed through to query_memory."""
    provider, calls = _make_active_provider(provider_module, monkeypatch)

    provider.handle_tool_call(
        "forgetful_recall", {"query": "x", "project_ids": [1, 2]},
    )

    query_calls = [c for c in calls if c[0] == "query_memory"]
    assert query_calls[0][1].get("project_ids") == [1, 2]
    assert query_calls[0][1].get("strict_project_filter") is True


# ---------------------------------------------------------------------------
# forgetful_projects tool
# ---------------------------------------------------------------------------

def test_projects_tool_lists_when_called_with_no_args(provider_module, monkeypatch):
    projects = [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    provider, calls = _make_active_provider(
        provider_module, monkeypatch, projects=projects,
    )

    result = provider.handle_tool_call("forgetful_projects", {})

    payload = json.loads(result)
    assert any(c[0] == "list_projects" for c in calls)
    # The result should expose the project list in some form.
    flat = json.dumps(payload)
    assert '"name": "a"' in flat or "'name': 'a'" in flat


def test_projects_tool_creates_when_create_arg_provided(provider_module, monkeypatch):
    provider, calls = _make_active_provider(provider_module, monkeypatch)

    args = {"create": {"name": "newproj", "description": "a fresh project"}}
    provider.handle_tool_call("forgetful_projects", args)

    create_calls = [c for c in calls if c[0] == "create_project"]
    assert len(create_calls) == 1
    assert create_calls[0][1].get("name") == "newproj"
    assert create_calls[0][1].get("description") == "a fresh project"


# ---------------------------------------------------------------------------
# system_prompt_block ambient project list
# ---------------------------------------------------------------------------

def test_system_prompt_lists_available_projects(provider_module, monkeypatch):
    """When projects exist, the system prompt block lists their names+ids
    so the agent has ambient awareness when picking one for a save."""
    projects = [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    provider, _calls = _make_active_provider(
        provider_module, monkeypatch, projects=projects,
    )
    # Mimic post-initialize state: projects cached, hybrid mode, no cwd hint.
    provider._projects_cache = projects
    provider._cwd_project_id = None
    provider._recall_mode = "hybrid"

    block = provider.system_prompt_block()

    assert "alpha" in block
    assert "beta" in block


def test_system_prompt_marks_cwd_matched_project(provider_module, monkeypatch):
    """If the cwd's git remote matches a project, it should be marked in
    the prompt as a hint — not enforced, just a nudge."""
    projects = [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    provider, _calls = _make_active_provider(
        provider_module, monkeypatch, projects=projects,
    )
    provider._projects_cache = projects
    provider._cwd_project_id = 2
    provider._recall_mode = "hybrid"

    block = provider.system_prompt_block()

    # The cwd-matched project should carry some kind of marker that the
    # other project does not. Pin behaviour, not exact glyph.
    alpha_pos = block.find("alpha")
    beta_pos = block.find("beta")
    assert alpha_pos >= 0 and beta_pos >= 0
    # Look at the line containing 'beta' — it should contain a hint phrase
    # referencing cwd / current dir / matches that 'alpha' line does not.
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
