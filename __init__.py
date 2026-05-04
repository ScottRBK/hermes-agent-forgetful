"""Forgetful memory provider — semantic memory backed by the Forgetful MCP server.

Wraps a long-lived ``uvx forgetful-ai`` stdio subprocess and adapts it
to hermes-agent's ``MemoryProvider`` contract. Supports three recall
modes (``hybrid`` / ``context`` / ``tools``) and exposes six agent-callable
tools when tools are enabled.

Provider-agnostic: schemas use OpenAI function-calling shape so every
hermes provider adapter (Anthropic, OpenAI, Bedrock, Gemini, …) can
translate them. Do NOT add Claude-specific patterns here.

Reference:
- ABC contract: ``~/dev/hermes-agent/agent/memory_provider.py``
- Gold-standard plugin: ``~/dev/hermes-agent/plugins/memory/honcho/__init__.py``
- Backend transport: ``./client.py``
- Resolved config: ``./config.py``
"""

from __future__ import annotations

import sys as _sys

# ── Workaround for hermes' user-plugin loader (sibling-module pre-load bug) ──
# `~/.hermes/hermes-agent/plugins/memory/__init__.py::load_memory_provider`
# iterates ``provider_dir.glob("*.py")`` alphabetically and exec's each file
# in isolation BEFORE running our ``__init__.py``. When a sibling .py with
# a relative import (e.g. ``cli.py``: ``from .client import ...``) loads
# before its dependency, the exec raises ImportError — which the loader
# silently caches as an empty stub in ``sys.modules`` (the ``except Exception``
# only logs at debug level and never pops the broken entry). Any later
# ``from .cli import cmd_setup`` then resolves to that empty shell and
# raises "cannot import name 'cmd_setup'".
#
# Fix: evict every sibling-module entry under our package name BEFORE we
# touch any of them. Python's regular import machinery then loads each
# fresh in dependency order during our own top-level imports below.
for _sub in ("cli", "client", "config", "context_gather", "explore"):
    _sys.modules.pop(f"{__name__}.{_sub}", None)

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from agent.memory_manager import sanitize_context
from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home
from tools.registry import tool_error

from .client import ForgetfulClient, ForgetfulClientError
from .config import ForgetfulConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trivial-prompt detection (skip prefetch/sync for ack noise)
# ---------------------------------------------------------------------------

_TRIVIAL_PROMPT_RE = re.compile(
    r"^(?:yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|"
    r"lgtm|k)\.?$",
    re.IGNORECASE,
)


def _is_trivial_prompt(query: str) -> bool:
    """Return True for trivial acknowledgements — don't fire memory ops."""
    if not query:
        return True
    stripped = query.strip()
    if not stripped:
        return True
    if stripped.startswith("/"):
        return True  # slash command
    if len(stripped) <= 80 and _TRIVIAL_PROMPT_RE.match(stripped):
        return True
    return False


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling shape — provider-agnostic)
# ---------------------------------------------------------------------------

ENCODING_AGENT_TAG = "hermes-agent/forgetful-plugin"

RECALL_SCHEMA = {
    "name": "forgetful_recall",
    "description": (
        "Semantic search over the Forgetful knowledge base. Returns memories "
        "ranked by relevance with optional linked-memory context. Cross-project "
        "by default — set scope='current' to limit to the active project."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language search query.",
            },
            "query_context": {
                "type": "string",
                "description": "Brief explanation of WHY you're searching — improves reranking.",
            },
            "k": {
                "type": "integer",
                "description": "Number of primary results to return (1-20, default 5).",
            },
            "scope": {
                "type": "string",
                "enum": ["all", "current"],
                "description": "Project scope. 'all' (default) searches every project; 'current' restricts to the active project.",
            },
            "include_links": {
                "type": "boolean",
                "description": "When true, attach linked memories to each result for additional context (default true).",
            },
            "importance_min": {
                "type": "integer",
                "description": "Minimum importance score (1-10) for results.",
            },
        },
        "required": ["query"],
    },
}

SAVE_SCHEMA = {
    "name": "forgetful_save",
    "description": (
        "Persist a single atomic memory in Forgetful. Use for non-obvious "
        "decisions, durable patterns, surprising learnings, or facts worth "
        "recalling in future sessions. One concept per memory — keep it tight. "
        "Tagged with the active project unless scope='none'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short scannable title (5-200 chars).",
            },
            "content": {
                "type": "string",
                "description": "Memory body (max 2000 chars, ~300-400 words). One concept.",
            },
            "context": {
                "type": "string",
                "description": "WHY this matters / HOW it relates / WHAT implications (max 500 chars).",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Search keywords for semantic matching (max 10).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Categorization tags (max 10).",
            },
            "importance": {
                "type": "integer",
                "description": "Importance score (1-10, default 7). 9-10=foundational, 7-8=useful pattern.",
            },
            "scope": {
                "type": "string",
                "enum": ["current", "none"],
                "description": "Project scoping. 'current' (default) tags with the active project; 'none' leaves the memory unscoped (visible to all projects).",
            },
        },
        "required": ["title", "content", "context"],
    },
}

LINK_SCHEMA = {
    "name": "forgetful_link",
    "description": (
        "Manually link a memory to one or more related memories. Builds the "
        "knowledge graph beyond automatic embedding-similarity links."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "Source memory ID.",
            },
            "related_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Target memory IDs to link to the source.",
            },
        },
        "required": ["memory_id", "related_ids"],
    },
}

OBSOLETE_SCHEMA = {
    "name": "forgetful_obsolete",
    "description": (
        "Mark a memory as obsolete with an audit trail. Soft delete — "
        "the memory is preserved but excluded from default queries. "
        "Optionally point to a replacement via superseded_by."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "Memory ID to mark obsolete.",
            },
            "reason": {
                "type": "string",
                "description": "Why this memory is obsolete (audit trail).",
            },
            "superseded_by": {
                "type": "integer",
                "description": "Optional ID of the replacement memory.",
            },
        },
        "required": ["memory_id", "reason"],
    },
}


_BASIC_TOOL_SCHEMAS = [RECALL_SCHEMA, SAVE_SCHEMA, LINK_SCHEMA, OBSOLETE_SCHEMA]


EXPLORE_SCHEMA = {
    "name": "forgetful_explore",
    "description": (
        "Deep 5-phase traversal of the Forgetful knowledge graph for a "
        "topic — semantic entry, memory expansion, entity discovery, "
        "entity relationships, and entity-linked memories. Use when "
        "simple recall doesn't surface enough context, when investigating "
        "how concepts connect across projects, or when planning complex "
        "work that spans multiple topics. Returns a structured markdown "
        "graph report — synthesize from there."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "What to explore (concept, pattern, entity, decision area).",
            },
            "depth": {
                "type": "string",
                "enum": ["shallow", "medium", "deep"],
                "description": "shallow=phases 1-2 (quick), medium=phases 1-4 (default), deep=all five phases.",
            },
        },
        "required": ["topic"],
    },
}


GATHER_SCHEMA = {
    "name": "forgetful_gather_context",
    "description": (
        "Assemble multi-source implementation context for a planning or "
        "coding task. Pulls cross-project memories from Forgetful, follows "
        "linked code artifacts and documents, and (when CONTEXT7_API_KEY is "
        "set or the public endpoint is reachable) pulls framework docs from "
        "Context7 for the libraries you specify. Returns a structured "
        "six-section markdown report — synthesize it into your plan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Description of what you're about to plan or implement.",
            },
            "frameworks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Libraries / frameworks relevant to the task (e.g. 'fastapi', 'sqlalchemy'). Pulled from Context7 when available.",
            },
            "include_web": {
                "type": "boolean",
                "description": "Reserved for v1.1 — no-op in v1 (WebSearch not yet integrated).",
            },
        },
        "required": ["task"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class ForgetfulMemoryProvider(MemoryProvider):
    """Forgetful semantic memory provider for hermes-agent."""

    def __init__(self) -> None:
        self._config: Optional[ForgetfulConfig] = None
        self._client: Optional[ForgetfulClient] = None
        self._cron_skipped: bool = False
        self._session_id: str = ""
        self._hermes_home: str = ""
        self._agent_context: str = ""

        # Recall mode (set in initialize from config)
        self._recall_mode: str = "hybrid"

        # Per-turn caches
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        self._prefetch_thread_started_at: float = 0.0
        self._prefetch_result: str = ""
        self._sync_thread: Optional[threading.Thread] = None

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "forgetful"

    # -- availability ------------------------------------------------------

    def is_available(self) -> bool:
        """Provider is available when the ``uvx`` launcher is on PATH.

        The forgetful-ai package itself is fetched on first run by uvx,
        so we don't require it to be pre-installed.
        """
        import shutil
        return shutil.which("uvx") is not None

    # -- lifecycle ---------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Set up the stdio MCP client and resolve runtime config.

        Skips entirely under cron/flush execution contexts so scheduled
        agents don't pollute the user's KB with system-prompt-driven turns.
        """
        # Cron guard — must come first, mirrored from Honcho.
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in ("cron", "flush") or platform == "cron":
            logger.debug(
                "forgetful: skipped (agent_context=%s platform=%s)",
                agent_context, platform,
            )
            self._cron_skipped = True
            return

        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home") or str(get_hermes_home())
        self._agent_context = agent_context

        try:
            cfg = ForgetfulConfig.load(self._hermes_home)
        except Exception as exc:
            logger.warning("forgetful: config load failed: %s — plugin inactive", exc)
            return
        self._config = cfg
        self._recall_mode = cfg.recall_mode

        try:
            client = ForgetfulClient(
                command=cfg.forgetful_command,
                args=list(cfg.forgetful_args),
                env=cfg.subprocess_env(),
                startup_timeout=cfg.startup_timeout,
                call_timeout=cfg.call_timeout,
            )
            client.start()
        except ForgetfulClientError as exc:
            logger.warning(
                "forgetful: failed to start stdio client (%s) — plugin inactive", exc,
            )
            self._client = None
            return

        self._client = client
        logger.info(
            "forgetful: initialized (mode=%s project_id=%s backend=%s)",
            cfg.recall_mode, cfg.project_id, cfg.backend,
        )

    # -- config + setup ----------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Schema fields surfaced by ``hermes memory setup``.

        Recall mode, project bind, and the optional Context7 companion key.
        Database/transport tuning lives in ``forgetful.json`` — surfaced via
        the standalone ``hermes forgetful setup`` wizard, not the generic
        memory picker.
        """
        return [
            {
                "key": "recall_mode",
                "description": "Recall mode: hybrid (auto context + tools), context (auto-inject only), tools (CRUD only).",
                "default": "hybrid",
                "choices": ["hybrid", "context", "tools"],
            },
            {
                "key": "project_name",
                "description": "Forgetful project name (leave blank to skip project binding).",
            },
            {
                "key": "context7_api_key",
                "description": "Optional Context7 API key — enables library doc lookup inside forgetful_gather_context.",
                "secret": True,
                "env_var": "CONTEXT7_API_KEY",
                "url": "https://context7.com",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret config to ``$HERMES_HOME/forgetful.json``."""
        from .config import save_config_file
        save_config_file(values, hermes_home)

    def post_setup(self, hermes_home: str, config: Dict[str, Any]) -> None:
        """Delegate full provider setup to the cli.cmd_setup wizard."""
        from .cli import cmd_setup
        cmd_setup(hermes_home=hermes_home, hermes_config=config)

    def shutdown(self) -> None:
        """Wait for in-flight threads and stop the subprocess."""
        for t in (self._prefetch_thread, self._sync_thread):
            if t is not None and t.is_alive():
                try:
                    t.join(timeout=5.0)
                except Exception:
                    pass
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # -- helpers (used across tool/prefetch/sync paths) --------------------

    def _is_inactive(self) -> bool:
        return self._cron_skipped or self._client is None or not self._client.is_alive()

    def _project_ids_filter(self, scope: str) -> Optional[List[int]]:
        """Resolve a project_ids filter from a 'scope' arg.

        ``scope='current'`` → [project_id] when configured, else None.
        ``scope='all'`` → None (cross-project recall — the default).
        """
        if scope == "current" and self._config and self._config.project_id is not None:
            return [self._config.project_id]
        return None

    def _execute(self, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Call a forgetful MCP tool via the meta-tool dispatcher."""
        if self._client is None:
            raise ForgetfulClientError("forgetful client not initialized")
        return self._client.execute(
            "execute_forgetful_tool",
            {"tool_name": tool_name, "arguments": args},
        )

    # -- tools -------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas based on recall mode.

        ``context`` mode hides all tools (auto-injected context only).
        ``tools`` and ``hybrid`` modes both expose the full surface.

        Liveness is *deliberately* not checked here. Hermes-agent calls
        this from ``MemoryManager.add_provider`` to populate the dispatch
        routing table — which runs before ``initialize()`` starts the
        subprocess. Gating on ``_client`` would leave the table empty and
        the LLM unable to dispatch. The runtime liveness check belongs in
        ``handle_tool_call``, which already returns ``tool_error`` when
        the provider is inactive.
        """
        if self._recall_mode == "context":
            return []
        return list(_BASIC_TOOL_SCHEMAS) + [EXPLORE_SCHEMA, GATHER_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch a tool call to the matching handler.

        Always returns a JSON string (success payload or ``tool_error``).
        """
        if self._is_inactive():
            return tool_error("forgetful: provider not active")

        try:
            if tool_name == "forgetful_recall":
                return self._handle_recall(args)
            if tool_name == "forgetful_save":
                return self._handle_save(args)
            if tool_name == "forgetful_link":
                return self._handle_link(args)
            if tool_name == "forgetful_obsolete":
                return self._handle_obsolete(args)
            if tool_name == "forgetful_gather_context":
                return self._handle_gather_context(args)
            if tool_name == "forgetful_explore":
                return self._handle_explore(args)
            return tool_error(f"forgetful: unknown tool '{tool_name}'")
        except ForgetfulClientError as exc:
            logger.warning("forgetful: %s failed: %s", tool_name, exc)
            return tool_error(f"forgetful {tool_name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("forgetful: %s raised", tool_name)
            return tool_error(f"forgetful {tool_name}: {exc}")

    # -- per-tool handlers -------------------------------------------------

    def _handle_recall(self, args: Dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        if not query:
            return tool_error("forgetful_recall: 'query' is required")

        scope = args.get("scope") or "all"
        payload: Dict[str, Any] = {
            "query": query,
            "query_context": args.get("query_context") or "agent recall",
            "k": _clamp_int(args.get("k"), default=5, lo=1, hi=20),
            "include_links": bool(args.get("include_links", True)),
        }
        if (importance := args.get("importance_min")) is not None:
            payload["importance_threshold"] = _clamp_int(importance, default=1, lo=1, hi=10)

        proj_filter = self._project_ids_filter(scope)
        if proj_filter is not None:
            payload["project_ids"] = proj_filter
            payload["strict_project_filter"] = True

        result = self._execute("query_memory", payload)
        return json.dumps(result, default=str)

    def _handle_save(self, args: Dict[str, Any]) -> str:
        title = (args.get("title") or "").strip()
        content = (args.get("content") or "").strip()
        context = (args.get("context") or "").strip()
        missing = [
            n for n, v in (("title", title), ("content", content), ("context", context))
            if not v
        ]
        if missing:
            return tool_error(
                f"forgetful_save: missing required field(s): {', '.join(missing)}"
            )

        if len(content) > 2000:
            return tool_error(
                f"forgetful_save: content exceeds 2000 char limit (got {len(content)})"
            )

        scope = args.get("scope") or "current"
        keywords = _coerce_str_list(args.get("keywords"), max_len=10)
        tags = _coerce_str_list(args.get("tags"), max_len=10)

        payload: Dict[str, Any] = {
            "title": title[:200],
            "content": content,
            "context": context[:500],
            "keywords": keywords,
            "tags": tags,
            "importance": _clamp_int(args.get("importance"), default=7, lo=1, hi=10),
            "encoding_agent": ENCODING_AGENT_TAG,
        }
        if scope == "current" and self._config and self._config.project_id is not None:
            payload["project_ids"] = [self._config.project_id]

        result = self._execute("create_memory", payload)
        return json.dumps(result, default=str)

    def _handle_link(self, args: Dict[str, Any]) -> str:
        memory_id = _coerce_int(args.get("memory_id"))
        related = args.get("related_ids") or []
        if memory_id is None:
            return tool_error("forgetful_link: 'memory_id' must be an integer")
        if not isinstance(related, list) or not related:
            return tool_error("forgetful_link: 'related_ids' must be a non-empty list")
        related_ints: List[int] = []
        for value in related:
            coerced = _coerce_int(value)
            if coerced is None:
                return tool_error(f"forgetful_link: related id {value!r} is not an integer")
            related_ints.append(coerced)
        result = self._execute(
            "link_memories",
            {"memory_id": memory_id, "related_ids": related_ints},
        )
        return json.dumps(result, default=str)

    # -- system prompt block ----------------------------------------------

    def system_prompt_block(self) -> str:
        """Return a static, mode-adapted Forgetful header for the system prompt.

        Empty under cron/inactive. Cache-friendly: contains no per-turn
        context (live recall is injected via ``prefetch()``).
        """
        if self._is_inactive():
            return ""

        scope_line = self._scope_summary()
        tool_names = [s["name"] for s in self.get_tool_schemas()]
        tool_list = ", ".join(tool_names) if tool_names else "(no tools active)"

        if self._recall_mode == "context":
            body = (
                "Context-injection mode. Relevant memories are auto-prepended "
                "to each turn — no memory tools are exposed. To save a new memory "
                "or curate the knowledge base, use the `hermes forgetful` CLI."
            )
        elif self._recall_mode == "tools":
            body = (
                "Tools-only mode. No automatic context injection — call "
                f"{tool_list} to read or write memories on demand."
            )
        else:
            body = (
                "Hybrid mode. Relevant memories are auto-injected each turn, "
                "AND the following tools are available for explicit recall, "
                f"saving, linking, and curation: {tool_list}."
            )

        return (
            "# Forgetful Memory\n"
            f"Active. {body}\n"
            f"{scope_line}"
        ).rstrip()

    def _scope_summary(self) -> str:
        if not self._config:
            return ""
        if self._config.project_id is not None:
            project = self._config.project_name or f"id={self._config.project_id}"
            return (
                f"Active project: **{project}** — writes are tagged with this "
                "project; reads search across all projects unless you set scope='current'."
            )
        return (
            "No active project set. Writes are unscoped; reads search across "
            "all projects. Run `hermes forgetful setup` to bind this directory "
            "to a project."
        )

    # -- prefetch / queue_prefetch ----------------------------------------

    _PREFETCH_FETCH_BUDGET = 5.0       # max seconds for a sync prefetch fetch
    _PREFETCH_STALE_MULTIPLIER = 3.0   # background thread is stale after N×budget

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return recalled memory context for this turn (markdown).

        Returns empty string under cron, in tools-only mode, when the
        prompt is trivial, when the provider is inactive, or when no
        relevant memories are found. Otherwise pops a cached background
        result; falls back to a bounded synchronous fetch on cold start.
        """
        if self._is_inactive():
            return ""
        if self._recall_mode == "tools":
            return ""
        if _is_trivial_prompt(query):
            return ""

        # Wait briefly for a queued background fetch to settle.
        thread = self._prefetch_thread
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=self._PREFETCH_FETCH_BUDGET)
            except Exception:
                pass

        with self._prefetch_lock:
            cached = self._prefetch_result
            self._prefetch_result = ""

        if cached:
            return self._truncate_to_budget(cached)

        # No cached result — cold start: fire synchronously with bounded timeout.
        try:
            raw = self._execute(
                "query_memory",
                self._build_recall_payload(query, k=5),
            )
        except ForgetfulClientError as exc:
            logger.debug("forgetful: sync prefetch failed: %s", exc)
            return ""

        formatted = _format_recall_for_prompt(raw)
        return self._truncate_to_budget(formatted)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Spawn a background fetch whose result will be served by the next prefetch()."""
        if self._is_inactive():
            return
        if self._recall_mode == "tools":
            return
        if _is_trivial_prompt(query):
            return

        # Don't pile up — if a recent (non-stale) thread is still running, skip.
        if self._thread_is_live():
            return

        payload = self._build_recall_payload(query, k=5)

        def _run() -> None:
            try:
                raw = self._execute("query_memory", payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug("forgetful: queue_prefetch failed: %s", exc)
                return
            formatted = _format_recall_for_prompt(raw)
            with self._prefetch_lock:
                self._prefetch_result = formatted

        self._prefetch_thread_started_at = time.monotonic()
        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="forgetful-prefetch",
        )
        self._prefetch_thread.start()

    def _thread_is_live(self) -> bool:
        """True when the prefetch thread is running and not past its staleness window."""
        thread = self._prefetch_thread
        if thread is None or not thread.is_alive():
            return False
        elapsed = time.monotonic() - self._prefetch_thread_started_at
        return elapsed < self._PREFETCH_FETCH_BUDGET * self._PREFETCH_STALE_MULTIPLIER

    def _build_recall_payload(self, query: str, *, k: int) -> Dict[str, Any]:
        """Default payload for prefetch-driven query_memory calls.

        Cross-project by default — the user's accumulated KB across all
        projects is what makes recall valuable. CLI scope flags can
        override on individual searches.
        """
        return {
            "query": query.strip(),
            "query_context": "auto-prefetch for incoming turn",
            "k": k,
            "include_links": True,
            "max_links_per_primary": 3,
        }

    def _truncate_to_budget(self, text: str) -> str:
        """Truncate text to fit within configured context_tokens budget."""
        if not text or not self._config or not self._config.context_tokens:
            return text
        budget = self._config.context_tokens * 4  # conservative chars-per-token
        if len(text) <= budget:
            return text
        truncated = text[:budget]
        last_space = truncated.rfind(" ")
        if last_space > budget * 0.8:
            truncated = truncated[:last_space]
        return truncated + " …"

    def _handle_explore(self, args: Dict[str, Any]) -> str:
        from .explore import run_explore

        topic = (args.get("topic") or "").strip()
        if not topic:
            return tool_error("forgetful_explore: 'topic' is required")
        depth = (args.get("depth") or "medium").strip().lower()
        if depth not in ("shallow", "medium", "deep"):
            depth = "medium"
        if self._client is None or self._config is None:
            return tool_error("forgetful_explore: provider not initialized")
        report = run_explore(
            client=self._client, config=self._config, topic=topic, depth=depth,
        )
        return json.dumps({"report": report}, default=str)

    def _handle_gather_context(self, args: Dict[str, Any]) -> str:
        from .context_gather import run_gather

        task = (args.get("task") or "").strip()
        if not task:
            return tool_error("forgetful_gather_context: 'task' is required")
        frameworks = _coerce_str_list(args.get("frameworks"), max_len=8)
        if self._client is None or self._config is None:
            return tool_error("forgetful_gather_context: provider not initialized")
        report = run_gather(
            client=self._client,
            config=self._config,
            task=task,
            frameworks=frameworks,
            include_web=bool(args.get("include_web", False)),
        )
        return json.dumps({"report": report}, default=str)

    # -- sync_turn ---------------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist a completed turn as a low-importance memory (background).

        Sanitizes both halves, drops trivial acks, and joins any in-flight
        sync thread on a 5s budget before launching a new one. All errors
        are swallowed at debug level — a failed sync must never break the
        agent's reply path.
        """
        if self._is_inactive():
            return

        clean_user = sanitize_context(user_content or "").strip()
        clean_assistant = sanitize_context(assistant_content or "").strip()
        if not clean_user and not clean_assistant:
            return
        if _is_trivial_prompt(clean_user) and len(clean_assistant) < 200:
            return

        sid = session_id or self._session_id
        title = _make_turn_title(clean_user)
        content = _make_turn_content(clean_user, clean_assistant, max_total=2000)
        context = (
            f"Auto-captured turn from hermes session {sid or 'unknown'}. "
            "Low-importance backup of conversation context — promote to a "
            "higher-importance memory via forgetful_save when worth keeping."
        )[:500]

        payload: Dict[str, Any] = {
            "title": title,
            "content": content,
            "context": context,
            "keywords": [],
            "tags": ["turn-pair", "auto"],
            "importance": 5,
            "encoding_agent": ENCODING_AGENT_TAG,
        }
        if self._config and self._config.project_id is not None:
            payload["project_ids"] = [self._config.project_id]

        def _sync() -> None:
            try:
                self._execute("create_memory", payload)
            except Exception as exc:  # noqa: BLE001
                logger.debug("forgetful: sync_turn write failed: %s", exc)

        # Stale-thread join, then fire-and-forget daemon thread.
        if self._sync_thread is not None and self._sync_thread.is_alive():
            try:
                self._sync_thread.join(timeout=5.0)
            except Exception:
                pass
        self._sync_thread = threading.Thread(
            target=_sync, daemon=True, name="forgetful-sync",
        )
        self._sync_thread.start()

    def _handle_obsolete(self, args: Dict[str, Any]) -> str:
        memory_id = _coerce_int(args.get("memory_id"))
        reason = (args.get("reason") or "").strip()
        if memory_id is None:
            return tool_error("forgetful_obsolete: 'memory_id' must be an integer")
        if not reason:
            return tool_error("forgetful_obsolete: 'reason' is required")
        payload: Dict[str, Any] = {"memory_id": memory_id, "reason": reason}
        if (sup := _coerce_int(args.get("superseded_by"))) is not None:
            payload["superseded_by"] = sup
        result = self._execute("mark_memory_obsolete", payload)
        return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _clamp_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    coerced = _coerce_int(value)
    if coerced is None:
        return default
    return max(lo, min(coerced, hi))


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_recall_for_prompt(raw: Dict[str, Any]) -> str:
    """Render a query_memory response as compact markdown for system-prompt injection.

    Empty or no-results responses return ``""``. Linked memories are
    nested as bullet sub-items beneath their primary.
    """
    if not isinstance(raw, dict):
        return ""
    primaries = raw.get("primary_memories") or raw.get("results") or []
    if not isinstance(primaries, list) or not primaries:
        return ""

    linked_index: Dict[int, Dict[str, Any]] = {}
    for entry in raw.get("linked_memories") or []:
        if isinstance(entry, dict):
            mid = _coerce_int(entry.get("id"))
            if mid is not None:
                linked_index[mid] = entry

    lines: List[str] = ["## Recalled memories (forgetful)"]
    for memory in primaries:
        if not isinstance(memory, dict):
            continue
        title = (memory.get("title") or "(untitled)").strip()
        content = (memory.get("content") or "").strip()
        importance = memory.get("importance")
        mem_id = memory.get("id")
        header = f"- **{title}**"
        if importance is not None:
            header += f" — importance {importance}/10"
        if mem_id is not None:
            header += f" (id={mem_id})"
        lines.append(header)
        if content:
            snippet = content if len(content) <= 400 else content[:397] + "…"
            for body_line in snippet.splitlines():
                lines.append(f"  {body_line}")

        for link_id in (memory.get("linked_memory_ids") or [])[:3]:
            link = linked_index.get(_coerce_int(link_id) or -1)
            if not link:
                continue
            link_title = (link.get("title") or "(untitled)").strip()
            link_snippet = (link.get("content") or "").strip()
            if link_snippet:
                link_snippet = link_snippet if len(link_snippet) <= 200 else link_snippet[:197] + "…"
            lines.append(f"  - linked: *{link_title}* — {link_snippet}")

    return "\n".join(lines).strip()


def _make_turn_title(user_content: str) -> str:
    """Build a scannable title from the user's turn (≤200 chars)."""
    text = (user_content or "").strip().replace("\n", " ")
    if not text:
        return "(turn — assistant message only)"
    if len(text) <= 80:
        return f"Turn: {text}"
    return f"Turn: {text[:77]}…"


def _make_turn_content(user_content: str, assistant_content: str, *, max_total: int) -> str:
    """Pack a turn-pair into ≤max_total chars, keeping the user message intact when possible."""
    user = (user_content or "").strip()
    assistant = (assistant_content or "").strip()
    user_block = f"User: {user}" if user else ""
    assistant_block = f"Assistant: {assistant}" if assistant else ""
    sep = "\n\n" if user_block and assistant_block else ""
    full = f"{user_block}{sep}{assistant_block}"

    if len(full) <= max_total:
        return full

    # Truncate the longer half to fit.
    overhead = len(sep) + len("User: ") + len("Assistant: ")
    budget = max_total - overhead
    if budget <= 0:
        return full[:max_total]

    # Allocate proportionally; keep both halves meaningful.
    user_share = max(200, int(budget * 0.4))
    assist_share = budget - user_share
    if user_share > len(user):
        assist_share += user_share - len(user)
        user_share = len(user)
    if assist_share > len(assistant):
        user_share += assist_share - len(assistant)
        assist_share = len(assistant)

    truncated_user = user[:user_share]
    truncated_assistant = assistant[:assist_share]
    if user_share < len(user):
        truncated_user += "…"
    if assist_share < len(assistant):
        truncated_assistant += "…"

    parts = []
    if truncated_user:
        parts.append(f"User: {truncated_user}")
    if truncated_assistant:
        parts.append(f"Assistant: {truncated_assistant}")
    return "\n\n".join(parts)[:max_total]


def _coerce_str_list(value: Any, *, max_len: int) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value[:max_len]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Hermes plugin registration hook."""
    ctx.register_memory_provider(ForgetfulMemoryProvider())
