"""Forgetful memory provider — semantic memory backed by the Forgetful MCP server.

Wraps a long-lived ``uvx forgetful-ai`` stdio subprocess and adapts it
to hermes-agent's ``MemoryProvider`` contract. Supports three recall
modes (``hybrid`` / ``context`` / ``tools``) and exposes five agent-callable
tools when tools are enabled: Forgetful's three meta-tools
(``discover_forgetful_tools``, ``how_to_use_forgetful_tool``,
``execute_forgetful_tool``) plus two plugin-side compositions
(``forgetful_explore``, ``forgetful_gather_context``) that orchestrate
multi-step graph traversal and cross-source context assembly that the
meta surface alone can't express.

Provider-agnostic: schemas use OpenAI function-calling shape so every
hermes provider adapter (Anthropic, OpenAI, Bedrock, Gemini, …) can
translate them. Do NOT add Claude-specific patterns here.

Reference:
- ABC contract: ``~/dev/hermes-agent/agent/memory_provider.py``
- Gold-standard plugin: ``~/dev/hermes-agent/plugins/memory/honcho/__init__.py``
- Backend transport: ``./client.py``
- Resolved config: ``./config.py``
- Forgetful meta-tools source: ``~/dev/forgetful/app/routes/mcp/meta_tools.py``
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

# ---------------------------------------------------------------------------
# Forgetful meta-tool schemas (transcribed from forgetful-ai's MCP surface)
#
# These three tools mirror the meta-pattern Forgetful itself exposes —
# rather than us authoring curated wrappers around create_memory /
# query_memory / etc., the agent reaches every Forgetful capability via:
#
#     1. ``discover_forgetful_tools``    — list the live catalog
#     2. ``how_to_use_forgetful_tool``   — fetch detailed parameter docs
#     3. ``execute_forgetful_tool``      — invoke any underlying tool
#
# The descriptions below are static copies of the docstrings shipped by
# ``~/dev/forgetful/app/routes/mcp/meta_tools.py`` (with feature-flagged
# sections inlined so the agent always sees the full surface). Hermes
# requires schemas at ``add_provider`` time, before the MCP subprocess
# starts — see ``tests/test_tool_schemas.py`` for the regression that
# pins this contract — so dynamic ``list_tools()`` fetch is not viable.
# ---------------------------------------------------------------------------

DISCOVER_TOOLS_SCHEMA = {
    "name": "discover_forgetful_tools",
    "description": (
        "Discover the live catalog of Forgetful tools available via "
        "execute_forgetful_tool. Returns each tool's name, mutation flag, "
        "parameters and an example call — enough to call the tool one-shot "
        "without a follow-up how_to_use lookup. Use this when you need to "
        "do something with memory and aren't sure which underlying tool fits, "
        "or to see what's available beyond the most common operations.\n\n"
        "## Tool catalog (representative — call discover for the live list)\n\n"
        "**User Tools** — User profile and preferences\n"
        "- get_current_user, update_user_notes\n\n"
        "**Memory Tools** — Atomic knowledge storage (<400 words per memory)\n"
        "- create_memory: Store a single concept with auto-linking; supports provenance fields "
        "(source_repo, source_files, source_url, confidence, encoding_agent, encoding_version)\n"
        "- query_memory: Semantic search (use query_context for better ranking)\n"
        "- get_memory, update_memory, get_recent_memories\n"
        "- link_memories, unlink_memories\n"
        "- mark_memory_obsolete: Soft-delete with audit trail and optional superseded_by\n\n"
        "**Project Tools** — Organise memories by context/scope\n"
        "- create_project, get_project, list_projects, update_project, delete_project\n\n"
        "**Code Artifact Tools** — Reusable code snippets\n"
        "- create_code_artifact, get_code_artifact, list_code_artifacts, update_code_artifact, delete_code_artifact\n\n"
        "**Document Tools** — Long-form content (>300 words)\n"
        "- create_document, get_document, list_documents, update_document, delete_document\n\n"
        "**Entity Tools** — Real-world entities (people, orgs, devices)\n"
        "- create_entity, get_entity, list_entities, search_entities, update_entity, delete_entity\n"
        "- link_entity_to_memory, unlink_entity_from_memory\n"
        "- link_entity_to_project, unlink_entity_from_project, get_entity_memories\n"
        "- create_entity_relationship, get_entity_relationships, update_entity_relationship, delete_entity_relationship\n\n"
        "Some servers also expose Skill / File / Plan / Task categories — "
        "call discover_forgetful_tools() with no category to see the live set.\n\n"
        "## Workflow\n"
        "1. discover_forgetful_tools() → see the catalog (optionally filtered by category)\n"
        "2. how_to_use_forgetful_tool(tool_name) → full parameter docs for one tool\n"
        "3. execute_forgetful_tool(tool_name, arguments) → run it"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": (
                    "Optional category filter. Common categories: user, memory, "
                    "project, code_artifact, document, entity, linking. Some "
                    "servers expose more (skill, file, plan, task) depending on "
                    "feature flags — call with no category to see the live list."
                ),
            },
        },
    },
}

HOW_TO_USE_SCHEMA = {
    "name": "how_to_use_forgetful_tool",
    "description": (
        "Get detailed documentation for a specific Forgetful tool, including "
        "its JSON parameter schema, required vs optional fields, return shape, "
        "and examples. Call this when you've identified a tool via discover "
        "but need the exact arguments to pass to execute_forgetful_tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": (
                    "Name of the underlying Forgetful tool to look up "
                    "(e.g. 'create_memory', 'query_memory', 'create_project')."
                ),
            },
        },
        "required": ["tool_name"],
    },
}

EXECUTE_TOOL_SCHEMA = {
    "name": "execute_forgetful_tool",
    "description": (
        "Execute any registered Forgetful tool by name. Forgetful is a "
        "semantic memory system — use this to read, write, link, and curate "
        "memories, projects, documents, code artifacts, and entities.\n\n"
        "## Quick Start — One-Shot Examples\n\n"
        "**Memory operations:**\n"
        "- Search: execute_forgetful_tool(\"query_memory\", "
        "{\"query\": \"search terms\", \"query_context\": \"why searching\"})\n"
        "- Create: execute_forgetful_tool(\"create_memory\", "
        "{\"title\": \"Short title\", \"content\": \"Memory body (<2000 chars)\", "
        "\"context\": \"Why this matters\", \"keywords\": [\"kw1\"], "
        "\"tags\": [\"tag1\"], \"importance\": 7, \"project_ids\": [1]})\n"
        "- Create with provenance: add source_repo, source_files, "
        "confidence, encoding_agent fields to track origin\n"
        "- Update: execute_forgetful_tool(\"update_memory\", "
        "{\"memory_id\": 1, \"content\": \"new content\"})\n"
        "- Get: execute_forgetful_tool(\"get_memory\", {\"memory_id\": 1})\n"
        "- Link: execute_forgetful_tool(\"link_memories\", "
        "{\"memory_id\": 1, \"related_ids\": [2, 3]})\n"
        "- Obsolete: execute_forgetful_tool(\"mark_memory_obsolete\", "
        "{\"memory_id\": 42, \"reason\": \"outdated\", \"superseded_by\": 100})\n\n"
        "**Project organisation:**\n"
        "- List: execute_forgetful_tool(\"list_projects\", {})\n"
        "- Create: execute_forgetful_tool(\"create_project\", "
        "{\"name\": \"Project Name\", \"description\": \"What this is\", "
        "\"project_type\": \"development\"})\n"
        "- Query within project: pass \"project_ids\": [1] to query_memory\n\n"
        "**Entities (people, orgs, devices):**\n"
        "- Create: execute_forgetful_tool(\"create_entity\", "
        "{\"name\": \"Sarah Chen\", \"entity_type\": \"Individual\", "
        "\"notes\": \"Backend dev\", \"aka\": [\"Sarah\", \"S.C.\"]})\n"
        "- Search: execute_forgetful_tool(\"search_entities\", "
        "{\"query\": \"Sarah\"})  # searches name AND aka\n"
        "- Link to memory: execute_forgetful_tool(\"link_entity_to_memory\", "
        "{\"entity_id\": 1, \"memory_id\": 1})\n\n"
        "**Documents (long-form, >300 words):**\n"
        "- Create: execute_forgetful_tool(\"create_document\", "
        "{\"title\": \"Doc Title\", \"description\": \"Brief summary\", "
        "\"content\": \"Long content...\", \"document_type\": \"text\", "
        "\"project_id\": 1})\n\n"
        "**Code artifacts (reusable snippets):**\n"
        "- Create: execute_forgetful_tool(\"create_code_artifact\", "
        "{\"title\": \"Snippet\", \"description\": \"What it does\", "
        "\"code\": \"def f(): pass\", \"language\": \"python\", \"project_id\": 1})\n\n"
        "## Linking Best Practices — always link related items for discoverability\n"
        "- When creating documents, link atomic memories: pass document_ids on create_memory\n"
        "- When creating code artifacts, link to memories: pass code_artifact_ids on create_memory\n"
        "- Memory-to-memory: link_memories(memory_id, related_ids=[...])\n"
        "- Entity-to-memory: link_entity_to_memory(entity_id, memory_id)\n"
        "- Entity-to-project: link_entity_to_project(entity_id, project_id)\n\n"
        "Use discover_forgetful_tools(category?) for the live catalog and "
        "how_to_use_forgetful_tool(tool_name) for the complete parameter "
        "schema of any specific tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tool_name": {
                "type": "string",
                "description": (
                    "Name of the Forgetful tool to execute (e.g. 'create_memory', "
                    "'query_memory'). See discover_forgetful_tools for the live catalog."
                ),
            },
            "arguments": {
                "type": "object",
                "description": (
                    "Tool-specific arguments. Call how_to_use_forgetful_tool(tool_name) "
                    "for the complete parameter schema of a given tool."
                ),
            },
        },
        "required": ["tool_name", "arguments"],
    },
}


_META_TOOL_SCHEMAS = [
    DISCOVER_TOOLS_SCHEMA,
    HOW_TO_USE_SCHEMA,
    EXECUTE_TOOL_SCHEMA,
]


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

        # Project awareness: a session-scoped cache of available projects
        # and an optional cwd-based hint. Populated at initialize time and
        # refreshed when the agent creates a project mid-session. Used
        # only by ``system_prompt_block`` for ambient awareness — saves
        # always resolve the project at call time, never from this cache.
        self._projects_cache: List[Dict[str, Any]] = []
        self._cwd_project_id: Optional[int] = None

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
        self._refresh_projects_cache()
        self._cwd_project_id = self._detect_cwd_project_hint()
        logger.info(
            "forgetful: initialized (mode=%s projects=%d cwd_hint=%s backend=%s)",
            cfg.recall_mode, len(self._projects_cache), self._cwd_project_id, cfg.backend,
        )

    # -- config + setup ----------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Schema fields surfaced by ``hermes memory setup``.

        Recall mode and the optional Context7 companion key. There is no
        sticky project binding — the agent picks a project per-save.
        Database/transport tuning lives in ``forgetful.json`` — surfaced
        via the standalone ``hermes forgetful setup`` wizard, not the
        generic memory picker.
        """
        return [
            {
                "key": "recall_mode",
                "description": "Recall mode: hybrid (auto context + tools), context (auto-inject only), tools (CRUD only).",
                "default": "hybrid",
                "choices": ["hybrid", "context", "tools"],
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
        ``tools`` and ``hybrid`` modes both expose the full surface: the
        three Forgetful meta-tools plus two plugin-side compositions.

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
        return list(_META_TOOL_SCHEMAS) + [EXPLORE_SCHEMA, GATHER_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Dispatch a tool call to the matching handler.

        Meta-tool dispatch (``discover_forgetful_tools``,
        ``how_to_use_forgetful_tool``, ``execute_forgetful_tool``) is a
        verbatim forward to the MCP subprocess — the plugin adds no
        translation, validation, or argument rewriting on top. The two
        composition tools (``forgetful_explore``, ``forgetful_gather_context``)
        keep their dedicated handlers because they orchestrate multi-step
        workflows that the meta surface alone can't express.

        Always returns a JSON string (success payload or ``tool_error``).
        """
        if self._is_inactive():
            return tool_error("forgetful: provider not active")

        try:
            if tool_name in ("discover_forgetful_tools", "how_to_use_forgetful_tool"):
                # Forgetful exposes these as direct MCP tools — no need to
                # wrap them under execute_forgetful_tool.
                result = self._client.execute(tool_name, args or {})
                return json.dumps(result, default=str)
            if tool_name == "execute_forgetful_tool":
                inner_name = args.get("tool_name")
                inner_args = args.get("arguments") or {}
                if not isinstance(inner_name, str) or not inner_name:
                    return tool_error(
                        "execute_forgetful_tool: 'tool_name' (string) is required"
                    )
                if not isinstance(inner_args, dict):
                    return tool_error(
                        "execute_forgetful_tool: 'arguments' must be an object"
                    )
                result = self._execute(inner_name, inner_args)
                return json.dumps(result, default=str)
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

    # -- system prompt block ----------------------------------------------

    def system_prompt_block(self) -> str:
        """Return a static, mode-adapted Forgetful header for the system prompt.

        Empty under cron/inactive. Cache-friendly: contains no per-turn
        context (live recall is injected via ``prefetch()``). Lists the
        available projects so the agent can pick one when filing a memory —
        projects are not sticky; every save is an explicit per-call
        decision passed via ``execute_forgetful_tool``'s arguments.
        """
        if self._is_inactive():
            return ""

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

        projects_block = self._format_projects_block()

        parts = [
            "# Forgetful Memory",
            f"Active. {body}",
        ]
        if projects_block:
            parts.append(projects_block)
        return "\n".join(parts).rstrip()

    def _format_projects_block(self) -> str:
        """Render the available-projects list for the system prompt.

        Empty when no projects exist or tools are hidden (context-only mode
        — agent can't act on the list anyway). Marks the cwd-matched
        project with ``← matches current directory`` so the agent knows
        which one is the most likely target without us forcing the choice.
        """
        if self._recall_mode == "context":
            return ""
        if not self._projects_cache:
            return (
                "## Forgetful projects\n"
                "_No projects yet. Call `execute_forgetful_tool` with "
                "`tool_name=\"create_project\"` and `arguments={\"name\": ..., "
                "\"description\": ..., \"project_type\": \"development\"}` "
                "to add one before saving project-scoped memories."
            )
        lines = ["## Forgetful projects"]
        for p in self._projects_cache:
            if not isinstance(p, dict):
                continue
            pid = p.get("id")
            name = p.get("name") or "(unnamed)"
            marker = " ← matches current directory" if pid == self._cwd_project_id else ""
            lines.append(f"- **{name}** (id={pid}){marker}")
        lines.append(
            "Saves default to global (no project). To file a memory under "
            "one of the above, call `execute_forgetful_tool` with "
            "`tool_name=\"create_memory\"` and pass `project_ids: [<id>]` "
            "in `arguments`."
        )
        return "\n".join(lines)

    def _refresh_projects_cache(self) -> None:
        """Pull the current project list and cache it for the prompt block.

        Errors are swallowed at debug level — the cache stays empty and
        the prompt simply tells the agent there are no projects yet.
        Called once at initialize. The cache will go stale if the agent
        creates a new project via ``execute_forgetful_tool`` mid-session;
        addressing that drift is deferred to a follow-up pass.
        """
        if self._client is None:
            return
        try:
            res = self._execute("list_projects", {})
        except Exception as exc:  # noqa: BLE001
            logger.debug("forgetful: project cache refresh failed: %s", exc)
            return
        projects = res.get("projects") if isinstance(res, dict) else None
        if isinstance(projects, list):
            self._projects_cache = [p for p in projects if isinstance(p, dict)]

    def _detect_cwd_project_hint(self) -> Optional[int]:
        """Return the id of a project whose name matches this cwd, if any.

        Reads the current working directory's git remote 'origin' URL,
        derives a candidate project name (e.g. ``foo`` from
        ``git@github.com:user/foo.git``), and returns the matching cached
        project's id. None when there's no remote, no match, or git is
        unavailable.
        """
        if not self._projects_cache:
            return None
        candidate = _project_name_from_cwd()
        if not candidate:
            return None
        for p in self._projects_cache:
            name = p.get("name") if isinstance(p, dict) else None
            if isinstance(name, str) and name == candidate:
                pid = _coerce_int(p.get("id"))
                if pid is not None:
                    return pid
        return None

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
            "higher-importance memory via execute_forgetful_tool with "
            "tool_name=create_memory when worth keeping."
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


def _project_name_from_cwd() -> Optional[str]:
    """Derive a candidate project name from the current cwd's git remote.

    Returns ``None`` when git isn't available, no remote is configured,
    or the URL doesn't parse. Used only as a soft hint for the system
    prompt — never as enforcement.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip()
    if not url:
        return None
    name = url.rstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    name = name.split("/")[-1]
    return name or None


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
