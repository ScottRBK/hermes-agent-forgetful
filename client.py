"""Stdio MCP subprocess wrapper for the Forgetful memory backend.

Spawns ``uvx forgetful-ai`` as a long-lived subprocess and exposes a
synchronous ``.execute(tool_name, arguments)`` facade over the async MCP
``ClientSession``. A dedicated background asyncio loop runs on a daemon
thread; sync callers submit coroutines via ``run_coroutine_threadsafe``
and block on the result with a per-call timeout.

Reference patterns:
- Async stdio client setup: ``~/dev/hermes-agent/tools/mcp_tool.py`` (_run_stdio)
- Subprocess cleanup with PID tracking: same file (_snapshot_child_pids)
- Forgetful CLI entry: ``~/dev/forgetful/main.py`` (forgetful / forgetful-ai → main:cli)
- Meta-tool surface: ``~/dev/forgetful/app/routes/mcp/meta_tools.py``
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import signal
import threading
from concurrent.futures import Future
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ForgetfulClientError(RuntimeError):
    """Raised when the stdio MCP subprocess cannot fulfil a request."""


class ForgetfulClient:
    """Synchronous facade over an async MCP stdio session to forgetful-ai.

    Lifecycle:
      client = ForgetfulClient()
      client.start()                     # spawns subprocess, performs MCP handshake
      result = client.execute("query_memory", {"query": "..."})
      client.close()                     # graceful shutdown
    """

    def __init__(
        self,
        command: str = "uvx",
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        startup_timeout: float = 60.0,
        call_timeout: float = 30.0,
    ) -> None:
        self._command = command
        self._args = args if args is not None else ["forgetful-ai"]
        self._env = env
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._session_ready = threading.Event()
        self._session_error: Optional[BaseException] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._session: Any = None
        self._closed = False
        self._lock = threading.Lock()
        self._pid: Optional[int] = None

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Spawn the subprocess and complete the MCP handshake.

        Blocks until the session is initialized or ``startup_timeout``
        elapses. Safe to call once; subsequent calls are no-ops.
        """
        with self._lock:
            if self._loop_thread is not None:
                return
            if self._closed:
                raise ForgetfulClientError("client has been closed")

            if shutil.which(self._command) is None:
                raise ForgetfulClientError(
                    f"required executable '{self._command}' not found on PATH"
                )

            self._loop_thread = threading.Thread(
                target=self._run_loop,
                name="forgetful-mcp-loop",
                daemon=True,
            )
            self._loop_thread.start()

        if not self._session_ready.wait(timeout=self._startup_timeout):
            self._abort_startup()
            raise ForgetfulClientError(
                f"forgetful-ai stdio session failed to initialize within "
                f"{self._startup_timeout:.0f}s"
            )

        if self._session_error is not None:
            err = self._session_error
            self._abort_startup()
            raise ForgetfulClientError(f"session init failed: {err}") from err

        atexit.register(self._atexit_cleanup)

    def _abort_startup(self) -> None:
        """Tear down a partial startup so the client is reusable / disposable."""
        loop = self._loop
        if loop is not None and loop.is_running() and self._stop_event is not None:
            try:
                loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        thread = self._loop_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._loop = None
        self._loop_thread = None
        self._session = None
        self._kill_pid()

    def close(self) -> None:
        """Signal the loop to exit and reap the subprocess."""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        loop = self._loop
        stop = self._stop_event
        if loop is not None and stop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass

        thread = self._loop_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)

        self._kill_pid()

    def _atexit_cleanup(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _kill_pid(self) -> None:
        pid = self._pid
        self._pid = None
        if pid is None:
            return
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                return

    # ---- public API --------------------------------------------------------

    def is_alive(self) -> bool:
        """Return True when the background loop and session are operational."""
        return (
            not self._closed
            and self._loop_thread is not None
            and self._loop_thread.is_alive()
            and self._session is not None
        )

    def execute(
        self,
        tool_name: str,
        arguments: Optional[dict[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Invoke an MCP tool and return its parsed structured result.

        Returns the structured payload when the tool exposes one (FastMCP
        wraps return-dicts as ``structured_content``); otherwise falls
        back to the first text content block parsed as JSON, then raw
        text wrapped under ``{"text": ...}``.

        Raises ``ForgetfulClientError`` on transport or tool error.
        """
        if self._closed:
            raise ForgetfulClientError("client is closed")
        if not self.is_alive():
            raise ForgetfulClientError("forgetful session is not active")

        loop = self._loop
        assert loop is not None  # guarded by is_alive()
        coro = self._call_tool(tool_name, arguments or {})
        future: Future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout or self._call_timeout)
        except TimeoutError as exc:
            future.cancel()
            raise ForgetfulClientError(
                f"call to '{tool_name}' timed out after "
                f"{timeout or self._call_timeout:.0f}s"
            ) from exc

    # ---- async internals (run inside the dedicated loop thread) -----------

    def _run_loop(self) -> None:
        """Run the dedicated asyncio loop hosting the MCP session."""
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._session_main())
        except BaseException as exc:  # noqa: BLE001
            self._session_error = exc
            self._session_ready.set()
            logger.exception("forgetful stdio session crashed: %s", exc)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None
            self._session = None

    async def _session_main(self) -> None:
        """Open the stdio transport and ClientSession, then wait for stop."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise ForgetfulClientError(
                "the 'mcp' Python SDK is required for the forgetful "
                "plugin (declare in plugin.yaml: pip_dependencies: [mcp])"
            ) from exc

        self._stop_event = asyncio.Event()
        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
        )

        # First-run UX hint: uvx may install forgetful-ai on first invocation.
        logger.info(
            "starting forgetful stdio subprocess (%s %s) — first run may "
            "take ~30-60s while uvx installs the package",
            self._command,
            " ".join(self._args),
        )

        async with stdio_client(params) as (read_stream, write_stream):
            self._capture_subprocess_pid()
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                self._session = session
                self._session_ready.set()
                logger.info("forgetful stdio session ready")
                await self._stop_event.wait()
                logger.info("forgetful stdio session stopping")
        self._session = None

    def _capture_subprocess_pid(self) -> None:
        """Best-effort: snapshot the spawned subprocess PID for force-kill on exit."""
        try:
            import psutil  # optional
            for child in psutil.Process(os.getpid()).children(recursive=False):
                if child.is_running():
                    self._pid = child.pid
                    return
        except Exception:
            pass

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        session = self._session
        if session is None:
            raise ForgetfulClientError("session unavailable")

        result = await session.call_tool(name, arguments)

        if getattr(result, "isError", False):
            text = _result_text(result) or "unknown error"
            raise ForgetfulClientError(f"tool '{name}' failed: {text}")

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            # FastMCP wraps non-dict returns under a "result" key when the
            # tool is type-hinted with a non-dict return; unwrap when present.
            if set(structured.keys()) == {"result"}:
                inner = structured["result"]
                return inner if isinstance(inner, dict) else {"result": inner}
            return structured

        text = _result_text(result)
        if text is None:
            return {}

        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return {"text": text}
        if isinstance(parsed, dict):
            return parsed
        return {"result": parsed}


def _result_text(result: Any) -> Optional[str]:
    """Extract the first text-content block from a CallToolResult."""
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return None
