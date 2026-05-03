"""Smoke test for ForgetfulClient against a real `uvx forgetful-ai` subprocess.

Run with: .venv-smoke/bin/python tests/smoke_client.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Add plugin root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import ForgetfulClient, ForgetfulClientError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def main() -> int:
    client = ForgetfulClient(startup_timeout=120.0)
    try:
        print("Starting ForgetfulClient (uvx forgetful-ai)…")
        client.start()
        print("Session is_alive:", client.is_alive())

        print("\n[1] discover_forgetful_tools (no args):")
        result = client.execute("discover_forgetful_tools", {})
        print("  total_count:", result.get("total_count"))
        print("  categories:", list((result.get("tools_by_category") or {}).keys()))

        print("\n[2] execute_forgetful_tool → list_projects:")
        result = client.execute(
            "execute_forgetful_tool",
            {"tool_name": "list_projects", "arguments": {}},
        )
        # Print compact preview
        preview = json.dumps(result, default=str)[:400]
        print(" ", preview)

        print("\n[3] how_to_use_forgetful_tool('query_memory'):")
        result = client.execute(
            "how_to_use_forgetful_tool",
            {"tool_name": "query_memory"},
        )
        keys = list(result.keys()) if isinstance(result, dict) else type(result).__name__
        print("  keys:", keys)

        print("\nAll smoke checks passed.")
        return 0
    except ForgetfulClientError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"UNEXPECTED: {exc!r}", file=sys.stderr)
        return 3
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
