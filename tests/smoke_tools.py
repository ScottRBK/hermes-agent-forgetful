"""Smoke test — exercise the four basic tools against a real forgetful-ai subprocess.

Run with: PYTHONPATH=/home/scott/dev/hermes-agent .venv-smoke/bin/python tests/smoke_tools.py
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/scott/dev/hermes-agent")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # ~/.hermes/plugins (parent of 'forgetful' pkg)

from forgetful import (  # noqa: E402
    ForgetfulMemoryProvider,
    _is_trivial_prompt,
    _BASIC_TOOL_SCHEMAS,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def expect(cond: bool, label: str) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        sys.exit(1)


def main() -> int:
    # --- pure logic checks (no subprocess needed) ---
    expect(_is_trivial_prompt("yes"), "_is_trivial_prompt: 'yes'")
    expect(_is_trivial_prompt("/help"), "_is_trivial_prompt: slash command")
    expect(not _is_trivial_prompt("Why did we choose Postgres?"), "_is_trivial_prompt: substantive query")

    expect(len(_BASIC_TOOL_SCHEMAS) == 4, "4 basic tool schemas defined")
    schema_names = {s["name"] for s in _BASIC_TOOL_SCHEMAS}
    expect(
        schema_names == {"forgetful_recall", "forgetful_save", "forgetful_link", "forgetful_obsolete"},
        f"schema names match: {schema_names}",
    )

    # --- live subprocess checks ---
    provider = ForgetfulMemoryProvider()
    hermes_home = tempfile.mkdtemp(prefix="forgetful-smoke-")
    print(f"\nHERMES_HOME: {hermes_home}")
    provider.initialize(session_id="smoke-1", hermes_home=hermes_home, platform="cli")

    expect(provider._client is not None, "client started")
    expect(provider.is_available(), "uvx on PATH")

    schemas = provider.get_tool_schemas()
    expect(len(schemas) == 4, f"hybrid mode returns 4 schemas (got {len(schemas)})")

    # forgetful_save
    save_args = {
        "title": "Forgetful plugin smoke test",
        "content": "Verifying that the forgetful_save dispatcher creates a memory via the stdio MCP subprocess.",
        "context": "Plugin development — proves the basic 4 tools wired correctly.",
        "keywords": ["smoke", "test", "forgetful", "plugin"],
        "tags": ["dev", "smoke-test"],
        "importance": 4,
        "scope": "none",
    }
    save_resp = provider.handle_tool_call("forgetful_save", save_args)
    save_obj = json.loads(save_resp)
    expect("memory" in save_obj or "id" in save_obj or "memory_id" in save_obj,
           f"forgetful_save returned memory payload: keys={list(save_obj)[:6]}")

    mem_id = (
        (save_obj.get("memory") or {}).get("id")
        or save_obj.get("id")
        or save_obj.get("memory_id")
    )
    expect(isinstance(mem_id, int), f"memory id extracted: {mem_id}")

    # forgetful_recall
    recall_resp = provider.handle_tool_call(
        "forgetful_recall",
        {"query": "smoke test for forgetful plugin", "k": 3, "scope": "all"},
    )
    recall_obj = json.loads(recall_resp)
    expect(
        "primary_memories" in recall_obj or "results" in recall_obj or "memories" in recall_obj,
        f"recall returned a result set: keys={list(recall_obj)[:6]}",
    )

    # forgetful_obsolete
    ob_resp = provider.handle_tool_call(
        "forgetful_obsolete",
        {"memory_id": mem_id, "reason": "smoke test cleanup"},
    )
    ob_obj = json.loads(ob_resp)
    expect(
        ob_obj.get("error") is None and not ob_obj.get("isError"),
        f"forgetful_obsolete success: {str(ob_obj)[:200]}",
    )

    # validation paths — should return tool_error JSON without raising
    err1 = json.loads(provider.handle_tool_call("forgetful_save", {"title": "x"}))
    expect("error" in err1 or err1.get("isError") or err1.get("status") == "error",
           f"missing fields → tool_error: {err1}")

    err2 = json.loads(provider.handle_tool_call("forgetful_link", {"memory_id": "abc"}))
    expect("error" in err2 or err2.get("isError") or err2.get("status") == "error",
           f"bad memory_id → tool_error: {err2}")

    # context mode hides tools
    provider._recall_mode = "context"
    expect(provider.get_tool_schemas() == [], "context mode hides tool schemas")
    provider._recall_mode = "hybrid"

    provider.shutdown()
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
