"""Tests for ``ForgetfulMemoryProvider.get_tool_schemas``.

These tests pin down the contract that hermes-agent's ``MemoryManager``
relies on: tool schemas describe the dispatch surface and must be returned
*at registration time*, before ``initialize()`` runs and starts the
subprocess.

The ordering, from ``hermes-agent/run_agent.py``:
  1. ``add_provider(provider)`` — calls ``get_tool_schemas()`` to populate
     ``_tool_to_provider`` (the dispatch routing table).
  2. ``initialize_all(...)`` — calls ``provider.initialize()`` which
     starts the stdio subprocess.
  3. ``get_all_tool_schemas()`` — re-queries to append schemas to
     ``self.tools`` (the LLM-visible list).

If ``get_tool_schemas()`` gates on ``_client is not None``, step 1 returns
[] and ``_tool_to_provider`` is permanently empty: the LLM may see the
schemas in step 3, but ``has_tool('forgetful_save')`` returns False so
dispatch falls through and the tool is never executed.

The fix lives in the *plugin*: liveness is the concern of ``handle_tool_call``,
not ``get_tool_schemas``. The schema is the contract; liveness is runtime.
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

# Make hermes-agent's modules (`agent.*`, `tools.*`, `hermes_constants`) importable.
if str(_HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(_HERMES_AGENT))


@pytest.fixture()
def provider_class():
    """Load the plugin's __init__.py the same way hermes-agent's loader does.

    Using ``spec_from_file_location`` with ``submodule_search_locations`` so
    relative imports (``from .client import ...``) resolve. A fresh package
    name per test prevents cross-test state leaks via ``sys.modules``.
    """
    pkg_name = "_test_forgetful_provider"

    # Evict prior load (and any submodules) so each test starts clean.
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
    return mod.ForgetfulMemoryProvider


_EXPECTED_TOOL_NAMES = {
    "discover_forgetful_tools",
    "how_to_use_forgetful_tool",
    "execute_forgetful_tool",
    "forgetful_explore",
    "forgetful_gather_context",
}


def test_uninitialized_provider_advertises_full_tool_surface(provider_class):
    """A provider that hasn't had ``initialize()`` called on it must still
    advertise its tool schemas.

    Hermes-agent calls ``get_tool_schemas`` from ``add_provider`` to build
    the dispatch table; that happens before ``initialize_all()`` runs and
    starts the subprocess. Returning [] here is what causes the LLM to
    see no forgetful tools.
    """
    provider = provider_class()
    # Deliberately NOT calling initialize() — _client is None.

    schemas = provider.get_tool_schemas()

    names = {s["name"] for s in schemas}
    assert names == _EXPECTED_TOOL_NAMES


def test_recall_mode_context_hides_all_schemas(provider_class):
    """In pure-context mode the agent gets auto-injected memory but no
    callable tools, so the schema list must stay empty."""
    provider = provider_class()
    provider._recall_mode = "context"

    assert provider.get_tool_schemas() == []


def test_recall_mode_tools_advertises_full_surface(provider_class):
    """Tools-only mode advertises everything (no auto-inject, but the agent
    can still call any tool)."""
    provider = provider_class()
    provider._recall_mode = "tools"

    names = {s["name"] for s in provider.get_tool_schemas()}
    assert names == _EXPECTED_TOOL_NAMES


def test_handle_tool_call_returns_error_when_inactive(provider_class):
    """Liveness lives in ``handle_tool_call``, not the schema list.

    Calling a tool on an un-initialized provider must return a structured
    ``tool_error`` JSON payload — not raise, not crash. This is the contract
    that lets ``get_tool_schemas`` safely advertise tools at registration time.
    """
    provider = provider_class()
    # _client is None — provider is inactive.

    result = provider.handle_tool_call(
        "execute_forgetful_tool",
        {"tool_name": "create_memory", "arguments": {}},
    )

    payload = json.loads(result)
    # tool_error returns a dict containing an error string mentioning the
    # provider name; we don't pin the exact format, just that it's an error.
    assert isinstance(payload, dict)
    flat = json.dumps(payload).lower()
    assert "error" in flat or "not active" in flat


def test_execute_forgetful_tool_forwards_to_client_verbatim(provider_class, monkeypatch):
    """``execute_forgetful_tool`` must hand its arguments straight through to
    the MCP client without any translation, validation, or rewriting.

    The plugin's job for the meta-tool surface is dispatch, not curation —
    if forgetful adds a new tool tomorrow, the agent should reach it via
    ``execute_forgetful_tool`` with no plugin change required.
    """
    provider = provider_class()

    class _StubClient:
        def is_alive(self):
            return True
        def close(self):
            pass

    provider._client = _StubClient()

    captured: list[tuple[str, dict]] = []
    def _fake_execute(tool_name, arguments):
        captured.append((tool_name, dict(arguments)))
        return {"id": 1, "ok": True}

    # The plugin's _execute method is the bridge to the client.
    monkeypatch.setattr(provider, "_execute", _fake_execute)

    result = provider.handle_tool_call(
        "execute_forgetful_tool",
        {
            "tool_name": "create_memory",
            "arguments": {"title": "T", "content": "C", "context": "X", "importance": 8},
        },
    )

    payload = json.loads(result)
    assert payload.get("ok") is True

    # Exactly one underlying call, with the inner tool name and verbatim args.
    assert len(captured) == 1
    inner_name, inner_args = captured[0]
    assert inner_name == "create_memory"
    assert inner_args == {"title": "T", "content": "C", "context": "X", "importance": 8}


def test_discover_and_how_to_use_forward_to_client(provider_class, monkeypatch):
    """``discover_forgetful_tools`` and ``how_to_use_forgetful_tool`` are
    plain forwards too — the plugin doesn't synthesize their results."""
    provider = provider_class()

    class _StubClient:
        def is_alive(self):
            return True
        def close(self):
            pass

    provider._client = _StubClient()

    captured: list[tuple[str, dict]] = []
    def _fake_client_execute(name, arguments=None):
        captured.append((name, dict(arguments or {})))
        return {"forwarded": True}

    # discover/how_to_use route directly through the client (not _execute,
    # which is the meta-dispatch wrapper for non-meta tools).
    provider._client.execute = _fake_client_execute  # type: ignore[attr-defined]

    discover_res = provider.handle_tool_call("discover_forgetful_tools", {"category": "memory"})
    how_res = provider.handle_tool_call("how_to_use_forgetful_tool", {"tool_name": "create_memory"})

    assert json.loads(discover_res).get("forwarded") is True
    assert json.loads(how_res).get("forwarded") is True

    assert ("discover_forgetful_tools", {"category": "memory"}) in captured
    assert ("how_to_use_forgetful_tool", {"tool_name": "create_memory"}) in captured
