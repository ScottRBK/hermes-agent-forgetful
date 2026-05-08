# Forgetful MCP Server Encoding Session

**Date:** 2026-05-06
**Repo:** `/home/scott/development/ai/mcp/forgetful`
**Profile:** medium (272 source files, 211 Python)
**Project:** forgetful (ID: 11)

## Results

- **Memories created:** 30 (target: 38-66 for medium — below target)
- **Entities created:** 18 (target: 8-15 ✓)
- **Entity relationships:** 14
- **Code artifacts:** 8 (target: 5-10 ✓)
- **Documents:** 1 (architecture reference)

## Key Findings

### Subagent delegation fails for memory creation
Attempted to delegate 8 memory creations to a subagent with `toolsets=["file"]`. Subagent failed because:
1. `forgetful_save` not in subagent's available tools
2. Filesystem tools all fail with stale `/development` path error
3. No mechanism for subagents to invoke MCP tools

**Lesson:** Always create memories directly in main agent context. Use parallel `forgetful_save` calls for batching.

### MCP project ID caching persists despite CLI switch
Ran `hermes hermes-agent-forgetful project switch forgetful` — CLI reported success, config updated. But MCP server still cached old project ID (13), so all 26 memories were tagged with wrong project. SQLite fix required post-encoding.

**Lesson:** CLI switch doesn't restart MCP server. Always plan for SQLite project association fix after encoding.

### Memory count below target
Produced 30 memories vs 38-66 target for medium profile. Contributing factors:
- 108 seconds wasted on failed subagent delegation attempt
- Single-threaded memory creation (could parallelize more aggressively)
- Some architecture memories could have been split further (e.g., separate memories for SQLite vs PostgreSQL backends)

### What worked well
- Direct SQLite insertion for entities/artifacts/documents worked perfectly
- Architecture reference document (11K chars) created successfully via SQLite
- Project notes update via SQLite worked (execute_forgetful_tool fallback)
- `execute_code` with absolute paths completely reliable for file discovery

## Repo Structure (Forgetful MCP Server)

```
app/
├── config/          # Settings, auth, logging
├── models/          # Pydantic schemas (memory, entity, project, skill, plan, etc.)
├── protocols/       # Repository interfaces (Protocol-based)
├── repositories/    # SQLite/Postgres + embeddings
├── services/        # Memory, Entity, Graph, Skill, Plan, Task, Activity, Backup, Re-Embedding
├── routes/          # MCP meta-tools + REST API
├── events/          # Event bus (pub/sub + SSE)
├── middleware/       # Auth, logging, token cache
└── utils/           # Token counter, provenance, pydantic helpers
```

## Session Notes

- User specifically requested encoding of the Forgetful MCP server repo (not the Hermes Agent plugin)
- This is the actual Forgetful codebase that powers the memory tools used by Hermes Agent
- Medium profile with 272 source files (211 Python, 41 markdown, 8 toml, 7 yaml)
- Key dependencies: FastMCP 3.2.2, FastAPI 0.129.0, SQLAlchemy 2.0, FastEmbed 0.7.4, Pydantic 2.12.5
- Supports both SQLite (aiosqlite + sqlite-vec) and PostgreSQL (asyncpg + pgvector) backends
- Feature flags control optional modules: SKILLS_ENABLED, PLANNING_ENABLED, FILES_ENABLED, RERANKING_ENABLED
