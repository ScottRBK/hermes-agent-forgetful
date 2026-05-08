# Hermes Agent Encoding Session

**Date:** 2026-05-06
**Repo:** `/home/scott/development/ai/third_parties/hermes-agent`
**Profile:** Large (2679 source files)
**Project:** hermes-agent (ID: 13, newly created)

## Results
- Memories: 30 (below large target of 66-112 — could have done more focused memories)
- Entities: 17 (within target 15-25)
- Code Artifacts: 8 (within target 8-15)
- Documents: 1 (architecture reference, 11.8K chars)
- Entity Relationships: 14

## What Worked Well
1. **Direct SQLite for entities/artifacts/documents** — This was the primary approach, not a fallback. All 17 entities, 8 code artifacts, and 1 document were created via direct SQLite. `execute_forgetful_tool` consistently fails for these operations.
2. **Project creation via SQLite** — The direct SQLite approach for creating a new project worked perfectly. Config was updated and MCP cache was fixed after encoding.
3. **forgetful_save for memories** — Reliable for memories under 2000 chars. Hit the limit 3 times before adjusting to ~1800 char targets.
4. **execute_code with absolute paths** — Essential for reliability. Terminal tool had stale working directory issues.

## Pitfalls Encountered
1. **MCP project ID caching** — After creating project 13, `forgetful_save` still tagged memories with project 12. Fixed via direct SQLite: deleted old associations and inserted new ones.
2. **forgetful_save 2000 char limit** — Hit this 3 times. Had to rewrite memories to be more concise. Aim for 1800 chars max.
3. **Sub-agent terminal issues** — Delegating to sub-agents failed with stale `/development` working directory. All work done via `execute_code` with absolute paths.

## Encoding Approach
1. Phase 0: Discovery with execute_code (os.walk, file reading)
2. Phase 1: Foundation memories (8 memories: identity, purpose, architecture, setup, technologies, agent loop, gateway, tool registry)
3. Phase 1B: Dependencies (1 memory: OpenAI SDK)
4. Phase 2: Architecture (8 memories: state store, context compression, skills, plugins, cron, delegation, TUI/ACP, config, credential pool)
5. Phase 2B: Entities (17 entities via direct SQLite, 14 relationships)
6. Phase 3: Patterns (4 memories: tool registry, persistent event loops, lazy import, atomic writes, error classification)
7. Phase 4: Features (4 memories: delegation, skills, browser, MCP, session search)
8. Phase 6: Code Artifacts (8 artifacts via direct SQLite)
9. Phase 7B: Architecture document (1 document, 11.8K chars via direct SQLite)
10. Project fix: Moved 30 memories from project 12→13, linked memory-entity pairs

## Lessons for Future Encodings
- For large repos, split architecture memories into smaller focused ones (e.g., one per subsystem) to hit memory targets
- Always use direct SQLite for entities/artifacts/documents — don't try execute_forgetful_tool first
- After creating a new project, ALWAYS fix project associations via SQLite at the end
- Test forgetful_save content length before saving — aim for 1800 chars max
- execute_code with absolute paths is the ONLY reliable approach for file operations
