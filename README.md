<p align="center">
  <img src="docs/images/banner.png" alt="Forgetful Hermes Plugin" width="600">
</p>

# Forgetful Memory Provider for Hermes

A [hermes-agent](https://github.com/NousResearch/hermes-agent) memory provider plugin backed by the [Forgetful](https://github.com/ScottRBK/forgetful) semantic-memory MCP server. Adds atomic Zettelkasten-style memories, knowledge-graph traversal, multi-source context gathering, and a multi-phase repo encoder to any hermes session — provider-agnostic, works with every model adapter hermes ships.

> **Status — alpha.** This is the user-installable preview ahead of the official `plugins/memory/forgetful/` PR to NousResearch/hermes-agent. Feedback welcome.

## What it gives you

- **Cross-session memory** — every hermes turn is auto-captured at low importance; agent-driven `execute_forgetful_tool` calls (`create_memory`, `link_memories`, …) for the things worth keeping.
- **Five tools the agent can call directly** — split into a meta-tier (verbatim forwards to Forgetful's full surface) and a composition tier (multi-phase orchestrations the meta surface can't express in one call):
  - `discover_forgetful_tools` — list available Forgetful tools by category.
  - `how_to_use_forgetful_tool` — fetch the docstring for any one tool.
  - `execute_forgetful_tool` — call any Forgetful tool by name with arbitrary arguments. New backend tools land for free; the plugin needs no change.
  - `forgetful_explore` — 5-phase deep graph traversal.
  - `forgetful_gather_context` — Forgetful + optional Context7, six-section markdown report.
- **Three recall modes** — `hybrid` (default: auto-injected context AND tools), `context` (auto-inject only), `tools` (CRUD only).
- **A standalone CLI** at `hermes forgetful {setup,status,projects,search,save,list,gather,explore,encode,reset}`.
- **A multi-phase repo encoder** that bootstraps a freshly-cloned project into the knowledge base.

## Install

### Prerequisites

You need [`uv`](https://docs.astral.sh/uv/) on your PATH (hermes uses it for plugin pip installs anyway, so you probably already do):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

That's the only prerequisite. The Forgetful backend itself is launched automatically via `uvx forgetful-ai` on first use — no separate server to manage.

### Install the plugin

Drop the plugin into your hermes plugins directory (or `git clone` it there):

```bash
cd ~/.hermes/plugins
git clone https://github.com/ScottRBK/hermes-forgetful forgetful
```

Then run hermes' setup wizard:

```bash
hermes memory setup
# pick "forgetful" from the list
```

The wizard:
1. Verifies `uvx` is available.
2. Asks for a recall mode (default `hybrid`).
3. Detects the git remote of the current directory and offers to create a matching Forgetful project (so the cwd-hint in the system prompt has something to point at).
4. Writes `~/.hermes/forgetful.json` (subprocess command, recall mode, backend choice — **no `project_id` / `project_name`**; project is decided per save).
5. Sets `memory.provider: forgetful` in your `~/.hermes/config.yaml`.

You're done — the next `hermes` invocation has memory.

## Verify

```bash
hermes forgetful status
```

Should print HERMES_HOME, the recall mode, the backend, and a successful "Connected — N backend tools available" probe along with a count of available projects.

## Daily use

### Inside a hermes session (hybrid / tools mode)

The agent sees the five tools and decides when to call them. Typical flow:

- You ask a question; before answering, the model auto-recalls relevant prior memories (injected as `<memory-context>`).
- The system prompt also lists the available projects (with a hint marking the cwd-matched one) so the model knows where to file new memories.
- The model calls `execute_forgetful_tool` with `tool_name="create_memory"` mid-conversation to persist a non-obvious decision (passing `project_ids: [<id>]` if it should be project-scoped).
- For complex planning, the model calls `forgetful_gather_context` and gets a six-section report (Memories / Code Patterns / Framework Guidance / Architectural Decisions / Knowledge Graph Insights / Implementation Notes).
- If it ever needs a tool it doesn't recognise, it calls `discover_forgetful_tools` / `how_to_use_forgetful_tool` to introspect the backend.

### From the shell

```bash
# Quick semantic search
hermes forgetful search "websocket reconnection backoff"

# Save a memory headlessly (or pipe in content)
hermes forgetful save --title "Pin Pydantic to 2.x" \
    --content "Pydantic 3 broke our Field validators." \
    --context "Upgrade attempted 2026-04, reverted."

# List recent memories in the project matching the current directory's git remote
hermes forgetful list --project current --limit 20

# 5-phase graph traversal of a topic
hermes forgetful explore "auth middleware patterns" --depth medium

# Context gather for a planning task
hermes forgetful gather "implement OAuth2 for FastAPI" --frameworks fastapi authlib
```

## Recall modes

Set in `~/.hermes/forgetful.json` or via `FORGETFUL_RECALL_MODE`:

| Mode | Auto-injected context | Tools exposed | When to use |
|---|---|---|---|
| `hybrid` *(default)* | yes | yes (all 5) | General use — model gets context AND can curate. |
| `context` | yes | no | When you want passive recall but a clean tool surface (e.g. agentic loops where extra tools confuse the model). |
| `tools` | no | yes (all 5) | When you want explicit-recall-only and prefer no auto-injection. |

## Project scoping

There is **no "active project" sticky state** — every save is an explicit per-call decision. The plugin's job is to keep the agent informed; the agent's job is to file memories in the right place.

- The system prompt block lists every Forgetful project with its id and name, and marks the one whose remote matches the current directory.
- To file a memory under a project, the agent passes `project_ids: [<id>]` in the `create_memory` arguments. To save globally, it omits the field.
- **Reads are cross-project by default** — this is the killer feature: when you're solving something in project A, you'll surface the time you solved it in project B. To narrow a query, the agent passes `project_ids: [<id>]` to `query_memory`.

To list / create projects from the shell:

```bash
hermes forgetful projects list
hermes forgetful projects create <name>
```

## Optional: Context7 companion

If you set `CONTEXT7_API_KEY` in your environment (or just rely on Context7's public read endpoint), `forgetful_gather_context` will pull library docs for any framework you mention. Get a key at https://context7.com — it's optional and the tool degrades silently when the key is unset or the network call fails.

## Optional: Postgres backend

By default the embedded `uvx forgetful-ai` subprocess uses SQLite at `~/.local/share/forgetful/forgetful.db`. To point at an existing Postgres instance instead, set in `~/.hermes/forgetful.json`:

```json
{
  "backend": "postgres",
  "postgres_host": "127.0.0.1",
  "postgres_port": 5099,
  "postgres_db": "forgetful",
  "postgres_user": "forgetful"
}
```

…and `FORGETFUL_POSTGRES_PASSWORD` in `~/.hermes/.env`.

## The repo encoder

When you start using Forgetful with an existing codebase, you want a one-shot bootstrap: walk the repo, extract foundation memories, build an entity graph, capture the canonical patterns and a handful of code artifacts. The encoder is exposed two ways — pick whichever fits your workflow:

**From the shell (CLI)** — for users:

```bash
# Dry-run first to see the rendered prompt
hermes forgetful encode ~/dev/my-project --dry-run

# Actually run it (spawns a hermes -z batch session)
hermes forgetful encode ~/dev/my-project --profile small
```

**From inside a hermes session (skill)** — for the agent:

The plugin ships an `encode-repo` skill, registered in `skills.external_dirs` by the setup wizard. Inside a hermes conversation you can ask the agent to encode a repo and it'll load the skill on demand:

> "Encode the current repo into Forgetful at the small profile."

Both paths share the same multi-phase pipeline (Foundation → Dependencies → Architecture → Entity Graph → Patterns → Features → Decisions → Code Artifacts → Architecture Document) — the CLI just spawns a clean `hermes -z` session that loads the same skill.

Profiles (small / small_complex / medium / large) tune the memory budget per phase. Auto-detected from source-file count if you omit the flag.

The encoder uses **whatever model your hermes is configured for** — no `--model` overrides, no model-tier selection, no Anthropic-specific patterns. It runs your configured model through eight (mostly mandatory) phases and prints a completion summary.

## Configuration matrix

All config flows through `~/.hermes/forgetful.json` (non-secret) and environment variables.

| Field | JSON key | Env var | Default |
|---|---|---|---|
| Recall mode | `recall_mode` | `FORGETFUL_RECALL_MODE` | `hybrid` |
| Context token cap | `context_tokens` | `FORGETFUL_CONTEXT_TOKENS` | `4000` |
| Subprocess command | `forgetful_command` | `FORGETFUL_COMMAND` | `uvx` |
| Subprocess args | `forgetful_args` | `FORGETFUL_ARGS` | `forgetful-ai` |
| Startup timeout (s) | `startup_timeout` | `FORGETFUL_STARTUP_TIMEOUT` | `60` |
| Per-call timeout (s) | `call_timeout` | `FORGETFUL_CALL_TIMEOUT` | `30` |
| Backend | `backend` | `FORGETFUL_BACKEND` | `sqlite` |
| Postgres host | `postgres_host` | `FORGETFUL_POSTGRES_HOST` | _(unset)_ |
| Postgres port | `postgres_port` | `FORGETFUL_POSTGRES_PORT` | _(unset)_ |
| Postgres db | `postgres_db` | `FORGETFUL_POSTGRES_DB` | _(unset)_ |
| Postgres user | `postgres_user` | `FORGETFUL_POSTGRES_USER` | _(unset)_ |
| Postgres password | _(secret — env only)_ | `FORGETFUL_POSTGRES_PASSWORD` | _(unset)_ |
| SQLite path | `sqlite_path` | `FORGETFUL_SQLITE_PATH` | platform default |
| Context7 API key | _(secret — env only)_ | `CONTEXT7_API_KEY` | _(unset)_ |

## Troubleshooting

**`uvx is not on PATH`** — install [uv](https://docs.astral.sh/uv/) and re-run `hermes forgetful setup`.

**First-run pause (~30–60s)** — `uvx` fetches `forgetful-ai` and downloads embedding models on first invocation. Subsequent runs reuse the cached venv.

**`forgetful-ai stdio session failed to initialize`** — bump `FORGETFUL_STARTUP_TIMEOUT=120` if your machine is slow on first install.

**Memory writes silently dropped** — confirm you're not in a cron/flush execution context (`hermes` invoked via `hermes cron run` or similar). The plugin intentionally skips writes from those contexts to avoid corrupting your KB with system-prompt-driven turns.

**Cross-project recall pulled in something irrelevant** — narrow the query by passing `project_ids: [<id>]` to `query_memory` (the agent does this via `execute_forgetful_tool`; the CLI does it via `--project current` on `search` / `list`).

**Subprocess hangs after agent exit** — the plugin registers an `atexit` handler that kills the subprocess on hermes shutdown. If something escapes, `pkill -f 'forgetful-ai'`.

## Architecture

```
┌─────────────────────────────────────┐
│ hermes-agent process                │
│                                     │
│  agent.MemoryManager                │
│       │                             │
│       ▼ (prefetch / sync_turn)      │
│  ForgetfulMemoryProvider            │
│       │                             │
│       ▼ (sync facade)               │
│  ForgetfulClient                    │
│       │                             │
│       │ (asyncio loop on            │
│       │  daemon thread)             │
│       ▼                             │
│  mcp.ClientSession                  │
└────────┬────────────────────────────┘
         │ stdio (JSON-RPC over pipes)
         ▼
┌─────────────────────────────────────┐
│ uvx forgetful-ai subprocess         │
│  · FastMCP server                   │
│  · 40+ memory tools                 │
│  · SQLite or Postgres backend       │
└─────────────────────────────────────┘
```

The plugin is provider-agnostic — every tool schema is OpenAI-shape so hermes' adapter layer (Anthropic / OpenAI / Bedrock / Gemini / NIM / Xiaomi MiMo / z.ai / etc.) translates them transparently. **No Claude-specific patterns. No model selection in the plugin.** See `docs/PLAN.md` §13a for the full provider-agnosticism contract.

## Roadmap → upstream PR

This repo is the user-installable preview. The plan is to validate it for a few weeks, then submit `feat(plugins/memory/forgetful): ...` to NousResearch/hermes-agent. See `docs/PLAN.md` §12 for the upstream-PR plan.

## License

MIT.
