# Hermes Forgetful Memory Provider — Implementation Plan

## 1. Goals & scope

**Phase 1 (this plan)**: Build a user-installed memory provider plugin for hermes-agent that wraps Forgetful (semantic memory MCP server) and ports the agentic context-retrieval workflow from context-hub-plugin. Validate end-to-end against running hermes-agent. Mirror to a public GitHub repo.

**Phase 2 (after Phase 1 validation)**: Submit official PR to `NousResearch/hermes-agent` adding the plugin to `plugins/memory/forgetful/`. Same code, plus tests, docs page, and a Conventional-Commits PR description.

**v1 surface**:
- 6 agent-callable tools
- 9 CLI subcommands (including `encode`)
- Three recall modes (context / tools / hybrid)
- Optional Context7 companion integration

---

## 2. Architecture decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| **Transport** | stdio MCP via `uvx forgetful-ai` subprocess (no pre-install needed) | Zero config burden — `uvx` resolves on first run, caches after. No separate server to manage. |
| **Recall modes** | `context` (auto-inject only) / `tools` (CRUD only) / `hybrid` (default — both) | Mirrors Honcho's pattern (B1) — adapts `system_prompt_block()` and `get_tool_schemas()` based on mode. |
| **Project scoping** | Writes tagged with current `project_id` (auto-detected from git remote in `post_setup`); reads **unscoped by default** | Preserves cross-project recall ("we already solved this" wins) — Pattern 3 from context-hub. CLI flag `--scope current` can opt into scoped reads. |
| **Context7** | Optional companion — only attempted inside `forgetful_gather_context` if `CONTEXT7_API_KEY` env var is set; skipped silently otherwise | Documented as a recommended-but-optional companion in README. |
| **Encode-repo** | CLI command only (`hermes forgetful encode <path>`) — spawns a hermes batch session with a packaged prompt that drives the multi-phase pipeline | Encode is fundamentally user-initiated, long-running, and benefits from a clean isolated agent session. Not exposed as a mid-conversation tool. |
| **Atomic memories vs turn-pairs** | v1: `sync_turn` writes turn-pair as a single low-importance memory (importance=5) tagged with project_id. Higher-importance atomic memories come from agent calling `forgetful_save` explicitly during conversation. | Avoids polluting the KB with verbose turn captures while still preserving session context. Curation is the agent's job, not the framework's. |

---

## 3. File layout

```
~/.hermes/plugins/forgetful/
├── __init__.py              # ForgetfulMemoryProvider + register(ctx)
├── plugin.yaml              # name, version, description, hooks list, pip_dependencies
├── README.md                # quickstart, config matrix, mode examples, troubleshooting
├── PLAN.md                  # this file (kept for reference during dev)
├── client.py                # stdio MCP client wrapper (subprocess lifecycle + meta-tool dispatch)
├── config.py                # config loading (~/.hermes/forgetful.json + env vars)
├── cli.py                   # hermes forgetful {setup,status,project,gather,search,save,list,explore,encode,reset}
├── context_gather.py        # the context-hub orchestration port (Forgetful + optional Context7)
├── encoder/
│   ├── __init__.py
│   ├── prompt.md            # multi-phase encoder prompt (ported from /encode-repo)
│   └── runner.py            # spawn hermes batch session with prompt + path
└── tests/                   # ad-hoc dev tests; full test suite arrives in Phase 2
    └── test_smoke.py
```

---

## 4. MemoryProvider ABC implementation map

Source of truth: `~/dev/hermes-agent/agent/memory_provider.py`. Honcho is the gold-standard reference (`~/dev/hermes-agent/plugins/memory/honcho/__init__.py`).

| ABC method | Forgetful implementation |
|---|---|
| `name` (property) | `"forgetful"` |
| `is_available()` | `shutil.which("uvx") is not None` (no network) |
| `initialize(session_id, **kwargs)` | Cron guard (skip if `agent_context in {"cron","flush"}` or `platform=="cron"`). Spawn `uvx forgetful-ai` subprocess via `client.py`. Load config. Resolve `project_id` from config. |
| `system_prompt_block()` | Mode-adapted markdown header. Empty if cron-skipped. Tools-only mode shows tool list; context-only mode says "context auto-injected"; hybrid mode shows both. |
| `prefetch(query, *, session_id)` | Trivial-prompt skip (`yes/no/ok/...` regex + slash-command skip). Daemon-thread search via `execute_forgetful_tool("query_memory", {...})`. **No `project_ids` filter** (cross-project recall). Token budget cap. Returns formatted markdown. |
| `queue_prefetch(query, *, session_id)` | Spawn background prefetch thread with stale-thread recovery (Honcho's pattern). |
| `sync_turn(user_content, assistant_content, *, session_id)` | Daemon thread + 5s join timeout pattern. **Critical**: `sanitize_context(user_content)` before storing. Calls `execute_forgetful_tool("create_memory", {...})` with importance=5, tagged with `project_ids=[current]` and `encoding_agent="hermes-agent/v1"`. |
| `get_tool_schemas()` | `[]` when mode is "context" or cron-skipped. Otherwise returns 6 schemas (see §5). |
| `handle_tool_call(name, args, **kwargs)` | Dispatches to per-tool handlers. All return JSON strings via `json.dumps()` or `tool_error()`. Catches all exceptions. |
| `on_session_end(messages)` | Wait for pending sync thread (10s timeout). Optionally extract end-of-session summary memory. |
| `on_session_switch(new_session_id, *, parent_session_id, reset, **kwargs)` | Update `_session_id`. If `reset=True`, flush per-session caches (none in v1, but plumbed for future). |
| `on_memory_write(action, target, content, metadata)` | Mirror built-in `add user` writes as Forgetful memories with importance=8. |
| `get_config_schema()` | Schema for `recall_mode`, `project_id`, `forgetful_endpoint` (optional), `context7_api_key` (optional secret). |
| `save_config(values, hermes_home)` | Write to `Path(hermes_home) / "forgetful.json"`. |
| `post_setup(hermes_home, config)` | Run wizard from `cli.cmd_setup` — detect git remote, prompt for project name, call `create_project` via stdio client, persist config. |
| `shutdown()` | Wait for pending threads (5s join). Kill subprocess. |

---

## 5. Tool surface (6 tools)

All tools return JSON strings. Schema descriptions explicit and parameter-tight.

### `forgetful_recall`
Semantic search across memories. Cross-project by default.
- `query` (string, required)
- `query_context` (string, optional — improves reranking)
- `k` (integer, default 5, max 20)
- `scope` (enum: `all` | `current` — default `all`)
- `include_links` (boolean, default true)
- `importance_min` (integer 1-10, optional)

### `forgetful_save`
Create a single atomic memory. Agent-driven curation.
- `title` (string, required, 5-200 chars)
- `content` (string, required, ≤2000 chars)
- `context` (string, required, ≤500 chars — the WHY)
- `keywords` (string[], max 10)
- `tags` (string[], max 10)
- `importance` (integer 1-10, default 7)
- `scope` (enum: `current` | `none` — default `current` — adds project_id)

### `forgetful_link`
Manually link two memories (build knowledge graph).
- `memory_id` (integer, required)
- `related_ids` (integer[], required)

### `forgetful_obsolete`
Soft-delete a memory with audit trail.
- `memory_id` (integer, required)
- `reason` (string, required)
- `superseded_by` (integer, optional)

### `forgetful_explore`
5-phase deep graph traversal report on a topic. Returns structured markdown.
- `topic` (string, required)
- `depth` (enum: `shallow` | `medium` | `deep` — default `medium`)

### `forgetful_gather_context`
The context-hub orchestration port. Forgetful (cross-project) + optional Context7. Returns markdown matching context-hub's six-section output contract.
- `task` (string, required — the implementation task or question)
- `frameworks` (string[], optional — hints for Context7 lookup)
- `include_web` (boolean, default false — invoke WebSearch fallback)

---

## 6. CLI surface

Wired via `cli.py`'s `register_cli(subparser)`. Only registered when `memory.provider == "forgetful"`.

| Command | Purpose |
|---|---|
| `hermes forgetful setup` | Wizard: detect git remote, prompt for project name, create forgetful project, choose recall mode, save config |
| `hermes forgetful status` | Health check: subprocess up? project_id set? recent memory count? mode? |
| `hermes forgetful project [show\|switch <name>\|list]` | Project management |
| `hermes forgetful gather <task>` | Run `context_gather.py` from shell, print markdown report |
| `hermes forgetful search <query> [--scope current\|all] [-k N]` | Quick semantic query |
| `hermes forgetful save` | Interactive prompt to create a memory (or `--from-clipboard`) |
| `hermes forgetful list [--limit N] [--project current]` | Browse recent memories |
| `hermes forgetful explore <topic> [--depth shallow\|medium\|deep]` | 5-phase graph traversal report |
| `hermes forgetful encode <path> [--profile small\|medium\|large]` | Multi-phase repo encoder. Spawns hermes batch session with packaged prompt + path. |
| `hermes forgetful reset` | Tear down config, prepare for re-run of setup |

---

## 7. Encoder design (`encoder/`)

The encoder is the largest deferred-from-context-hub component and the single biggest piece of v1.

### Approach
- `encoder/prompt.md` — the multi-phase pipeline as a long markdown prompt (port from `commands/encode-repo-serena.md`, stripped of Serena-specific symbol-level instructions, replaced with hermes-native Read/Glob/Grep equivalents)
- `encoder/runner.py` — given a target repo path, spawn a hermes batch session (`hermes -p <prompt> --cwd <path>`) with the encoder prompt
- The batch session's agent uses `forgetful_save`, `forgetful_link`, etc. (which are already registered because the plugin is active) to populate the KB
- Phase completion gates from the original are preserved as prompt-level instructions

### Phase mapping (stripped Serena dependency)
| Original phase | v1 hermes equivalent |
|---|---|
| Phase 0 Discovery | Glob for manifests + Read README |
| Phase 1 Foundation (5 memories + project notes) | Same — call `forgetful_save` 5 times, then update project notes via Forgetful `update_project` |
| Phase 1B Dependency Analysis | Parse manifests + (if Context7 available) validate major frameworks |
| Phase 2 Symbol-Level Architecture | **Replaced** with file-tree + Grep-based architecture summary (lossier than Serena but generally applicable) |
| Phase 2B Entity Graph (MANDATORY) | Same — call `execute_forgetful_tool("create_entity", {...})` and `create_entity_relationship` |
| Phase 3 Pattern Discovery | Same — Grep-based pattern queries |
| Phase 4 Critical Features | Same — feature identification + memory creation |
| Phase 5 Documentation-Only Decisions | Same — strict ADR/README-only sourcing |
| Phase 6 Code Artifacts (MANDATORY) | Same — `create_code_artifact` with extracted code snippets |
| Phase 6B Symbol Index Document | **Stripped** in v1 (requires Serena) — defer to v1.1 if Serena plugin lands |
| Phase 7 Additional Documents | Same |
| Phase 7B Architecture Reference Document | Same — `create_document` with synthesis |

### Profile-based memory targets (preserved from original)
| Profile | Memories | Entities | Artifacts | Documents |
|---|---|---|---|---|
| Small Simple | 17-31 | 3-5 | 3-5 | 1-2 |
| Small Complex | 28-46 | 5-8 | 4-7 | 2-3 |
| Medium Standard | 38-66 | 8-15 | 5-10 | 3-5 |
| Large | 66-112 | 15-25 | 8-15 | 5-10 |

### CLI command
```
hermes forgetful encode <path> [--profile small|medium|large] [--dry-run]
```
- `--profile` overrides auto-detection
- `--dry-run` shows the plan without making writes

---

## 8. Patterns lifted verbatim from Honcho (gold-standard reference)

Verified by reading `~/dev/hermes-agent/plugins/memory/honcho/__init__.py` (1329 lines).

1. **Cron guard**: `if agent_context in ("cron", "flush") or platform == "cron": self._cron_skipped = True; return` in `initialize()`. Every other method early-returns when `_cron_skipped`.
2. **Daemon thread + 5s join timeout** in `sync_turn`:
   ```python
   if self._sync_thread and self._sync_thread.is_alive():
       self._sync_thread.join(timeout=5.0)
   self._sync_thread = threading.Thread(target=_sync, daemon=True, name="forgetful-sync")
   self._sync_thread.start()
   ```
3. **Stale-thread recovery** in `queue_prefetch`: treat threads older than `timeout × 2` as dead so hung calls don't block new fires (`_thread_is_live()` helper).
4. **`sanitize_context()`** before storing turn content (`from agent.memory_manager import sanitize_context`) — strips `<memory-context>` fences so we don't persist prior turn's prefetched context.
5. **Trivial prompt detection**: regex `^(yes|no|ok|okay|sure|thanks|thank you|y|n|...)$` + slash-command skip. Don't fire prefetch on these.
6. **Token budget enforcement** on returned context (Honcho uses `context_tokens × 4` char approximation).
7. **`get_tool_schemas() → []` in context-only mode** (so the agent sees no tools, just auto-injected context).
8. **`post_setup()` delegates to `cli.cmd_setup()`** wizard — keep the actual setup logic in cli.py for reuse from `hermes forgetful setup`.
9. **`get_hermes_home()` from `hermes_constants`** for all path operations — never hardcode `~/.hermes`. Use `display_hermes_home()` for user-facing messages.
10. **All exceptions caught in handlers**: log at `debug` level, return `tool_error()` JSON. Never raise.

---

## 9. Implementation order (tasks)

Each step independently testable. After step N completes, smoke test before proceeding.

1. **`client.py`** — stdio MCP subprocess wrapper. Spawn `uvx forgetful-ai`, manage lifecycle, expose `.execute(tool_name, args) → dict`. Handle subprocess crash / timeout / restart. **Riskiest piece — validate first.**
2. **`config.py`** — load/save `~/.hermes/forgetful.json` + env var overrides. Project_id, recall_mode, context7_api_key.
3. **`__init__.py` skeleton** — `ForgetfulMemoryProvider` class with `name`, `is_available()`, `initialize()` (with cron guard), `shutdown()`. `register(ctx)` at bottom.
4. **Tool layer (basic 4)** — `get_tool_schemas()` (mode-aware), `handle_tool_call()` for `forgetful_recall` + `forgetful_save` + `forgetful_link` + `forgetful_obsolete`. Return JSON strings.
5. **`sync_turn()`** — daemon thread pattern, sanitize_context, importance=5 turn-pair memory.
6. **`prefetch()`** + **`queue_prefetch()`** — daemon-thread search, trivial-prompt skip, token budget, cached-result pattern.
7. **`system_prompt_block()`** — mode-adapted text.
8. **`get_config_schema()` + `save_config()` + `post_setup()`** — wizard with git-remote detection.
9. **`cli.py` core commands** — setup, status, project, search, save, list, reset.
10. **`context_gather.py`** + **`forgetful_gather_context` tool** — orchestrated retrieval with optional Context7.
11. **`forgetful_explore` tool** + `hermes forgetful explore` CLI — 5-phase graph traversal.
12. **`encoder/prompt.md`** + **`encoder/runner.py`** + `hermes forgetful encode` CLI — multi-phase encoder.
13. **README.md** — quickstart, config matrix, mode examples, troubleshooting, encoder usage, Context7 companion docs.
14. **`plugin.yaml`** — final metadata with hooks list and pip_dependencies hint.
15. **GitHub repo** — push to `github.com/ScottRBK/hermes-forgetful` (or chosen name), README + LICENSE (MIT).

---

## 10. Validation plan

After step 15, end-to-end validation:

1. Verify prereq: `which uvx` returns a path
2. Set `memory.provider: forgetful` in `~/.hermes/config.yaml`
3. Run `hermes forgetful setup` from inside `~/dev/hermes-agent` — verify project created on Forgetful side, config.json written
4. Run `hermes` interactively, do 3-5 turns of substantive conversation
5. Verify turn-pair memories appear via `hermes forgetful list`
6. Restart `hermes`, verify next session's `prefetch` surfaces relevant prior turns
7. Have agent call `forgetful_save` for an atomic memory — verify it lands with importance=7+
8. Run `hermes forgetful gather "implement WebSocket reconnection with exponential backoff"` — verify cross-project memories surface in markdown report
9. Run `hermes forgetful explore "memory provider patterns"` — verify 5-phase report
10. Switch `recall_mode` between context/tools/hybrid — verify tool schemas and system prompt change
11. Test cron guard: run `hermes` with `--cron` flag, verify no Forgetful writes occur
12. Run `hermes forgetful encode ~/dev/some-small-repo --profile small --dry-run` — verify plan shows expected phases
13. Run actual encode on a small repo — verify all mandatory phase gates met (entities, artifacts, foundation memories)

---

## 11. Open verification items (resolve during implementation)

These need source-level verification when we hit them — don't speculate now:

1. **Stdio meta-tool surface**: confirm `uvx forgetful-ai` (no args) exposes the 3 meta-tools and that `execute_forgetful_tool` covers everything. If stdio surfaces direct tools too, we can call them directly without the dispatch hop.
2. **MCP Python SDK choice**: `mcp` (Anthropic official) vs `fastmcp.client` — pick whichever shares install footprint with forgetful's deps to minimize total install.
3. **Subprocess robustness**:
   - First-run UX (`uvx` install pause, ~5-10s) — log a clear "First run, installing forgetful-ai…" message
   - Crash mid-session: auto-restart? mark inactive? Log and degrade gracefully.
   - Zombie processes on hard hermes crash: PID file? `atexit` handler? Both?
4. **Hermes batch session API**: confirm `hermes -p <prompt> --cwd <path>` is the right way to spawn an isolated agent for the encoder. May need `--profile` or other flags.
5. **`sanitize_context`**: confirm it's importable from `agent.memory_manager` (Honcho imports it directly — should work for us).

---

## 12. Phase 2 PR plan (high-level only — finalize after Phase 1 ships)

After Phase 1 validates and we've used it ourselves for a few weeks:

1. **Fork** `NousResearch/hermes-agent`, create branch `feat/forgetful-memory-provider`
2. **Move plugin** to `plugins/memory/forgetful/` (no code changes — same module structure)
3. **Add tests** under `tests/plugins/memory/forgetful/` mocking the stdio client (use `_isolate_hermes_home` autouse fixture, work with `pytest -n auto`)
4. **Add docs page** at `website/docs/developer-guide/forgetful-memory-provider.md` modeled on the existing memory-provider-plugin doc
5. **Update README pointer** in `plugins/memory/README.md` (if such an index exists)
6. **Verify no core file changes** (the Teknium rule from PR #5295) — provider is self-contained
7. **PR description** uses Conventional Commits format: `feat(plugins/memory/forgetful): add Forgetful semantic memory provider`. Body: motivation, architecture summary, modes, link to validation evidence (this repo), Context7 companion note, Encoder pipeline summary, screenshots/asciinema of `gather` and `encode` flows
8. **Engage** with maintainers in the PR thread — be responsive, accept review feedback, don't push back on style preferences

---

## 13. Out of scope for v1 (explicit non-goals)

- Serena LSP integration (Phase 6B symbol index document) — defer until/unless a Serena hermes plugin exists
- Learn-from-rejection patterns for save curation — too speculative for v1
- Auto-extraction of atomic memories from turn content (LLM-based curation) — for v1, the agent is responsible for explicit `forgetful_save` calls; turn-pair backup is just safety net
- Multi-tenant / per-user-id project isolation — v1 is single-user; gateway scenarios later

---

## 13a. CRITICAL: Hermes is provider-agnostic — do NOT add model-specific optimizations

**Stop and read this before writing any code.**

Hermes-agent supports 14+ model providers (Nous Portal, OpenRouter, Anthropic, OpenAI, Bedrock, Gemini, NVIDIA NIM, Xiaomi MiMo, z.ai/GLM, Kimi/Moonshot, MiniMax, Hugging Face, custom endpoints, more). The user can swap providers at any time via `hermes model`. **Anything you write must work for all of them.**

The earlier context-hub-plugin (Claude Code only) used Claude-specific patterns that DO NOT TRANSLATE to hermes:

| ❌ Claude-Code pattern (do NOT replicate) | ✅ Hermes-correct approach |
|---|---|
| Spawning a haiku-tier subagent for cheap graph traversal | Use the same model the user configured. If exploration is expensive, narrow the work — don't switch models. |
| Hardcoding Anthropic tool-use formats | Hermes adapters (anthropic_adapter, bedrock_adapter, gemini_native_adapter, etc.) translate OpenAI-shape tool calls to provider native. Always emit OpenAI-format schemas in `get_tool_schemas()`. |
| Anthropic prompt-caching tags / cache_control hints | Don't touch. Hermes' caching is provider-aware and managed centrally. |
| Extended thinking blocks / Anthropic-specific reasoning fields | Out of scope — provider adapters handle reasoning shape. |
| Sonnet-vs-haiku model selection in subagents | Don't pick models. Period. |
| Claude-specific system prompt conventions | `system_prompt_block()` returns plain markdown — works for every provider. |

**Specific traps in this plan to avoid**:
- §5 `forgetful_explore` and `forgetful_gather_context`: when these tools internally need expensive operations (graph traversal, multi-source synthesis), do them as Python in the plugin process, NOT by spawning sub-LLM-calls with model preferences. If the agent itself wants to delegate, it does so through hermes' `delegate_tool` (which respects user-configured model) — but the plugin should not invoke or assume any specific model.
- §7 encoder: the encoder spawns a hermes batch session via `hermes -p <prompt> --cwd <path>`. That session uses whatever model the user has configured. **Do not pass `--model` flags or otherwise override the user's choice.**
- Tool schema descriptions: write them for any LLM, not "Claude will understand…" or "ask Claude to…". Use neutral language ("the agent", "the model").
- Tool result formatting: don't use Claude-specific markdown extensions. Plain markdown only.

**If you find yourself thinking "this would be cheaper with haiku" or "we could use cache_control here" — STOP. You are violating provider-agnosticism. Find a non-model-specific optimization or accept the cost.**

---

## 13b. Reference material — where to find authoritative source-of-truth

**Use these. Do not invent function signatures, endpoint paths, or method names from memory.** When in doubt, Read the file.

### Hermes-agent (target framework)

Local clone: `~/dev/hermes-agent/`

| What you need | Where to look |
|---|---|
| The MemoryProvider ABC contract (every method signature, docstring, kwargs) | `~/dev/hermes-agent/agent/memory_provider.py` — read in full before implementing any method |
| MemoryManager orchestrator (how prefetch/sync/tool-routing actually work) | `~/dev/hermes-agent/agent/memory_manager.py` — especially `sanitize_context`, `prefetch_all`, `sync_all` |
| **Gold-standard reference plugin** | `~/dev/hermes-agent/plugins/memory/honcho/__init__.py` (1329 lines) — when in doubt about a pattern, this is the answer |
| Other reference plugins (for variety) | `~/dev/hermes-agent/plugins/memory/{mem0,supermemory,byterover,hindsight,holographic,openviking,retaindb}/__init__.py` |
| Plugin discovery mechanics | `~/dev/hermes-agent/plugins/memory/__init__.py` (how plugins are loaded), `~/dev/hermes-agent/hermes_cli/plugins.py` (PluginContext) |
| CLI subcommand registration | `~/dev/hermes-agent/plugins/memory/honcho/cli.py` — see `register_cli(subparser)` |
| Setup wizard contract | `~/dev/hermes-agent/hermes_cli/memory_setup.py` |
| Profile-aware path helpers | `~/dev/hermes-agent/hermes_constants.py` — `get_hermes_home()`, `display_hermes_home()` |
| Tool error helper | `~/dev/hermes-agent/tools/registry.py` — `tool_error(message)` returns a JSON-string error result |
| Contribution standards | `~/dev/hermes-agent/CONTRIBUTING.md` (PR conventions, branch naming, code style) |
| Plugin rules + Teknium "no core changes" rule | `~/dev/hermes-agent/AGENTS.md` (search for "Plugins") |
| Memory provider plugin docs | `~/dev/hermes-agent/website/docs/developer-guide/memory-provider-plugin.md` |
| Config schema for memory section | `~/dev/hermes-agent/hermes_cli/config.py` (search `memory:`) |

### Forgetful (the backend we're wrapping)

Local clone: `~/dev/forgetful/`

| What you need | Where to look |
|---|---|
| HTTP route definitions (request/response schemas) | `~/dev/forgetful/app/routes/api/memories.py`, `projects.py`, `entities.py`, `documents.py`, `code_artifacts.py` |
| MCP tool definitions (the meta-tool surface and the wrapped operations) | `~/dev/forgetful/app/routes/mcp/meta_tools.py` and sibling files in `app/routes/mcp/` |
| Pydantic models (canonical schema for Memory, Project, Entity, etc.) | `~/dev/forgetful/app/models/` |
| Service-layer logic (auto-linking thresholds, token budget enforcement) | `~/dev/forgetful/app/services/memory_service.py` |
| Settings / env vars | `~/dev/forgetful/app/config/settings.py` |
| Server entry point (transport options, port defaults) | `~/dev/forgetful/main.py` |
| Auth middleware | `~/dev/forgetful/app/middleware/auth.py` |
| First-run / install flow | `~/dev/forgetful/README.md` |

### Forgetful semantic memory (your own KB — query via the MCP tools available in this session)

Highly relevant existing memories to query before writing any non-trivial design code. Use `query_memory` with `project_ids=[64]` for hermes-agent specifically, or unscoped for cross-project recall:

- Memory #1538 — hermes-agent Plugin System (general + memory + context_engine)
- Memory #1551 — Plugin Rule (no hardcoded plugin logic in core) — Teknium May 2026
- Memory #1555 — Persistent Memory System (the updated, accurate ABC summary)
- Memory #1567 — Pluggable Context Engine (parallel pattern for reference)
- Memory #1569 — Decision: Plugins must not touch core (PR #5295)
- Memory #1528 — hermes Architecture (Agent + Gateway + Tools)
- Memory #1535 — hermes Tool Registry and Toolsets
- Memory #1548 — Closed Learning Loop (Skills + Memory + Curator)
- Memory #37, #42, #59, #77, #79 — Forgetful project overview, MCP integration, auto-linking, meta-tools pattern, token budget

Plus the Forgetful UI and Forgetful project memories under `project_ids=[4]` and `[20]` for backend implementation details.

### context-hub-plugin (the workflow we're porting)

GitHub: `https://github.com/ScottRBK/context-hub-plugin` (default branch is `master`, NOT `main`)

| What you need | Where to look |
|---|---|
| The context-retrieval subagent prompt | `agents/context-retrieval.md` — port the four-source strategy and output-contract markdown template (BUT see §13a — strip Claude-specific model references) |
| `/context_gather` command | `commands/context_gather.md` |
| `/encode-repo-serena` multi-phase pipeline | `commands/encode-repo-serena.md` (1308 lines) — port the phase structure, strip Serena LSP-specific instructions |
| Skills (Forgetful tool reference, curation, exploration) | `skills/using-forgetful-memory/SKILL.md`, `skills/using-forgetful-memory/TOOL_REFERENCE.md`, `skills/curating-memories/SKILL.md`, `skills/exploring-knowledge-graph/SKILL.md` |

Fetch via `gh api repos/ScottRBK/context-hub-plugin/contents/<path>` or raw.githubusercontent.com URLs.

### Verification protocol — when implementing each task

Before writing code for a task in §9:
1. **Read the relevant ABC method docstring** in `~/dev/hermes-agent/agent/memory_provider.py`
2. **Read Honcho's implementation of the same method** in `~/dev/hermes-agent/plugins/memory/honcho/__init__.py`
3. **Read the relevant Forgetful endpoint or MCP tool** in `~/dev/forgetful/app/routes/`
4. **Query Forgetful memory** for any existing decisions on this surface
5. **Then write the code** — citing the source files in any non-obvious comment

If a function name, kwarg, endpoint path, or env var feels uncertain, **grep the source first**. Never invent.

---

## 14. Quick-reference for post-compaction implementation

If conversation context is lost after compaction, re-read in this order:
1. This PLAN.md (top to bottom — pay extra attention to §13a and §13b)
2. `~/dev/hermes-agent/agent/memory_provider.py` (the ABC contract)
3. `~/dev/hermes-agent/plugins/memory/honcho/__init__.py` (gold-standard reference)
4. `~/dev/forgetful/main.py` + `~/dev/forgetful/app/routes/api/memories.py` (Forgetful API)
5. The Forgetful project memories listed in §13b

Then begin at task #1 in §9.
