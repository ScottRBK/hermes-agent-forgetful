"""Regression test: gateway load order with CLI scan running first.

Reproduces the live failure mode observed in agent.log:

    WARNING plugins.memory: Memory provider 'hermes-agent-forgetful'
            loaded but no provider instance found

The runtime ordering inside hermes-agent's CLI startup is:

  1. ``discover_plugin_cli_commands()`` runs during argparse setup. It loads
     the plugin's ``cli.py`` standalone via
     ``spec_from_file_location(<dotted_name>, str(cli_file))`` — without
     passing ``submodule_search_locations``. To make ``from .client import ...``
     resolve under that dotted name, ``cli.py`` synthesises a parent stub
     and registers it in ``sys.modules`` (see ``cli.py:34-39``).

  2. Later, the gateway calls ``load_memory_provider(...)`` which in turn
     calls ``_load_provider_from_dir``. That loader checks
     ``if module_name in sys.modules`` (``plugins/memory/__init__.py:203``)
     — and finds the stub that ``cli.py`` left behind. It uses the stub
     as ``mod``, never executes the real ``__init__.py``, and so finds
     no ``register`` function and no ``MemoryProvider`` subclass on the
     stub. ``_load_provider_from_dir`` returns ``None``.

The contract this test pins down: after ``cli.py`` has been pre-loaded
by hermes-agent's CLI scan, ``load_memory_provider`` must still return a
working ``ForgetfulMemoryProvider`` instance with a populated tool surface.

The fix lives in the *plugin*'s ``cli.py``: it must clean up the stub
once its own relative imports have resolved, so the later memory-loader
call sees a clean ``sys.modules`` and runs ``__init__.py`` properly.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_HERMES_AGENT = Path(os.environ.get("HERMES_AGENT_DIR", "/home/scott/.hermes/hermes-agent"))

if not (_HERMES_AGENT / "plugins" / "memory" / "__init__.py").exists():
    pytest.skip(
        "hermes-agent not available — set HERMES_AGENT_DIR to enable",
        allow_module_level=True,
    )

if str(_HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(_HERMES_AGENT))


@pytest.fixture()
def install_layout(tmp_path):
    """Build a minimal user-plugin install tree: ``<tmp>/plugins/<name>/``.

    Hermes-agent's loader keys on the directory name under ``plugins/``, so
    we use the canonical install name ``hermes-agent-forgetful`` and symlink
    each plugin file in (avoids copying ~30k of source).
    """
    name = "hermes-agent-forgetful"
    plugins = tmp_path / "plugins"
    install = plugins / name
    install.mkdir(parents=True)

    for src in _PLUGIN_DIR.iterdir():
        if src.name in {"tests", "__pycache__", ".git"}:
            continue
        (install / src.name).symlink_to(src)

    return install


def _import_loader(hermes_home, plugin_name):
    """Load hermes-agent's ``plugins.memory`` loader pointed at *hermes_home*.

    Stubs the two host-environment lookups the loader performs so the
    fixture is hermetic: ``_get_user_plugins_dir`` (filesystem scan)
    and ``_get_active_memory_provider`` (config.yaml read).
    """
    # Evict so a later real-gateway run isn't poisoned by our test fixture.
    for key in list(sys.modules):
        if key.startswith("plugins.memory") or key.startswith("_hermes_user_memory"):
            del sys.modules[key]

    spec = importlib.util.spec_from_file_location(
        "plugins.memory",
        str(_HERMES_AGENT / "plugins" / "memory" / "__init__.py"),
        submodule_search_locations=[str(_HERMES_AGENT / "plugins" / "memory")],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plugins.memory"] = mod
    spec.loader.exec_module(mod)

    user_plugins = hermes_home / "plugins"
    mod._get_user_plugins_dir = lambda: user_plugins  # type: ignore[attr-defined]
    mod._get_active_memory_provider = lambda: plugin_name  # type: ignore[attr-defined]
    return mod


def test_provider_loads_after_cli_scan_pre_populates_sys_modules(install_layout, tmp_path):
    """If ``discover_plugin_cli_commands`` has already loaded ``cli.py``,
    the subsequent ``load_memory_provider`` call must still return a real
    provider — not ``None``.

    This is the direct reproduction of the live agent.log warning.
    """
    hermes_home = install_layout.parent.parent  # tmp_path
    pm = _import_loader(hermes_home, "hermes-agent-forgetful")

    # Step 1: simulate gateway argparse scan.
    pm.discover_plugin_cli_commands()

    # Step 2: simulate gateway's memory init.
    provider = pm.load_memory_provider("hermes-agent-forgetful")

    assert provider is not None, (
        "Expected a ForgetfulMemoryProvider instance after CLI scan; "
        "got None — the loader is reusing a poisoned sys.modules entry "
        "left by cli.py."
    )

    schemas = provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert "execute_forgetful_tool" in names
    assert "discover_forgetful_tools" in names
    assert "how_to_use_forgetful_tool" in names
    assert "forgetful_explore" in names
    assert "forgetful_gather_context" in names
    assert len(names) == 5


def test_cli_scan_alone_does_not_leave_parent_stub_in_sys_modules(install_layout, tmp_path):
    """After ``discover_plugin_cli_commands`` finishes, the parent package
    name must NOT remain bound to a stub-only module in ``sys.modules``.

    The stub is an implementation detail of ``cli.py``'s relative-import
    workaround; once the imports resolve, the entry must be either removed
    or replaced with the real package — otherwise the next loader call
    short-circuits on it.
    """
    hermes_home = install_layout.parent.parent
    pm = _import_loader(hermes_home, "hermes-agent-forgetful")

    pm.discover_plugin_cli_commands()

    parent = "_hermes_user_memory.hermes-agent-forgetful"
    stub = sys.modules.get(parent)

    if stub is not None:
        # If the parent is still in sys.modules, it must be the REAL package
        # (i.e. it has the plugin's ForgetfulMemoryProvider) — not a bare stub.
        assert hasattr(stub, "ForgetfulMemoryProvider"), (
            f"Parent {parent!r} in sys.modules is a bare stub; "
            "_load_provider_from_dir will reuse it and return None."
        )
