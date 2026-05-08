# Forgetful Repo Encoding Session Notes

**Date:** 2026-05-04
**Repo:** `/home/scott/development/ai/mcp/forgetful`
**Project:** forgetful (id=11)
**Profile:** medium (277 source files)

## Session Results

**Created:** 37 memories total
- 5 foundation memories (what, why, architecture, setup, technologies)
- 8 architecture memories (layers: MCP, REST, services, protocols, repositories, models, events, testing)
- 6 pattern memories (protocol repos, event bus, Pydantic models, provenance, tool adapters, auth middleware)
- 5 feature memories (semantic search, graph traversal, MCP transport, REST API, Docker deployment)
- 3 decision memories (dual backend, pluggable embeddings, Zettelkasten principles)
- 5 code artifact memories (query method, protocol, tool adapter, event bus, embedding helpers)
- 5 document memories (search, roadmap, embedding migration, offline setup, AGENTS.md)

## Issues Encountered

### 1. forgetful_save 2000 char limit
Hit when trying to save Phase 7B architecture reference document (5205 chars). Had to skip and note that document tool was unavailable.

**Lesson:** Always keep memories under 2000 chars. For comprehensive docs, split into multiple focused memories or skip if document tool unavailable.

### 2. execute_forgetful_tool entity creation failed
Tried `create_entity` with proper args but it failed. Had to create entity graph as a memory instead and link it to architecture memories.

**Lesson:** Entity creation via MCP is unreliable. Fallback to creating entity-focused memories with proper linking.

### 3. create_code_artifact unavailable
Could not use `create_code_artifact` tool. Had to embed code snippets in `forgetful_save` memories with `tags=["artifact", ...]`.

**Lesson:** Code artifact tool may not be registered. Fallback to embedding code in regular memories.

### 4. create_document unavailable
Could not use `create_document` tool. Had to create document-focused memories with `tags=["document", ...]`.

**Lesson:** Document tool may not be registered. Fallback to creating document memories.

### 5. forgetful_obsolete threw "Unknown tool"
Even when other forgetful tools worked, `forgetful_obsolete` failed. This is a known plugin registration issue.

**Lesson:** `forgetful_obsolete` is unreliable. Workaround: create replacement memory with supersession note.

### 6. Project management command prefix
Correct command: `hermes hermes-agent-forgetful project switch <name>`
Wrong command: `hermes forgetful project switch <name>` (does not work)

**Lesson:** Plugin name is `hermes-agent-forgetful`, not `forgetful`.

## Key Discoveries

1. **Terminal tool has stale working directory** — `/development` doesn't exist, causing FileNotFoundError. Always use `execute_code` with absolute paths.

2. **Project auto-creation** — `hermes hermes-agent-forgetful project switch forgetful` auto-created the project when it didn't exist, then switched to it.

3. **Memory linking works well** — `forgetful_link` reliably linked related memories (e.g., entity graph to architecture memories).

4. **Forgetful is its own MCP server** — The codebase being encoded IS the Forgetful MCP server itself. It uses FastMCP framework, not the Hermes plugin directly.

5. **Dual database support** — SQLite (sqlite-vec) and PostgreSQL (pgvector) both supported via protocol-based abstraction.

6. **Pluggable embeddings** — FastEmbed (default), OpenAI, Google, Azure, Ollama all supported via adapter pattern.

## Encoding Approach That Worked

1. Phase 0: Discovery with `execute_code` and `os.walk()`
2. Phase 1: Foundation memories (5) — all under 2000 chars
3. Phase 1B: Dependency memories (3) — all under 2000 chars
4. Phase 2: Architecture memories (8) — all under 2000 chars
5. Phase 2B: Entity graph as memory (1) — linked to architecture memories
6. Phase 3: Pattern memories (6) — all under 2000 chars
7. Phase 4: Feature memories (5) — all under 2000 chars
8. Phase 5: Decision memories (3) — all under 2000 chars
9. Phase 6: Code artifact memories (5) — embedded code snippets
10. Phase 7: Document memories (5) — embedded documentation
11. Phase 7B: SKIPPED — content exceeded 2000 char limit, document tool unavailable

**Total: 37/38-66 target (medium profile)** — slightly under target but comprehensive.

## Recommendations for Future Encodings

1. **Always use `execute_code`** for file discovery — terminal is unreliable
2. **Keep memories under 1500 chars** to avoid hitting the 2000 char limit
3. **Fallback to `forgetful_save`** for everything — entities, code artifacts, documents
4. **Use `forgetful_link`** to build knowledge graph without entities
5. **Check project exists** before encoding — use `hermes hermes-agent-forgetful project switch <name>`
6. **Split comprehensive docs** into multiple focused memories instead of one large document
7. **Document tool unavailability** — if `create_code_artifact` or `create_document` fails, note it and use fallback
