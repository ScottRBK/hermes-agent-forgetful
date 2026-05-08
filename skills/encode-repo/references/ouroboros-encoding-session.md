# Ouroboros Encoding Session — 2026-05-06

## Summary
- **Repo:** `/home/scott/development/ai/ouroboros`
- **Profile:** medium (253 source files — 146 .py, 69 .json, 14 .js)
- **Project:** ouroboros (id: 12, created new)
- **Memories:** 22 (below medium target of 38-66)
- **Entities:** 8, Relationships: 8
- **Code Artifacts:** 5
- **Documents:** 4

## Issues Encountered

### 1. Project Creation via CLI Doesn't Exist
`hermes hermes-agent-forgetful project create ouroboros` fails with:
```
error: argument project_command: invalid choice: 'create' (choose from 'show', 'list', 'switch')
```
**Workaround:** Created project directly via SQLite INSERT + config file update.

### 2. Terminal Tool Stale Working Directory
Terminal tool gets stuck on `/development` directory causing all commands to fail with `FileNotFoundError`.
**Workaround:** Used `execute_code` with Python and absolute paths for everything.

### 3. forgetful_save Caches Old Project ID
After creating project 12 and updating `~/.hermes/forgetful.json`, all memories created via `forgetful_save` were tagged with project 11 (forgetful) instead of project 12 (ouroboros).
**Root cause:** MCP server caches project ID at startup, doesn't re-read config on each call.
**Fix:** After encoding, ran SQL to move memories from project 11 to project 12.

### 4. execute_forgetful_tool Not Registered
Entity creation, code artifacts, and document creation via MCP failed or had wrong arguments.
**Solution:** Used direct SQLite insertion for all entities, code artifacts, and documents. This was the most reliable approach.

### 5. Entities Table Has No project_ids Column
The skill's example code used `project_ids` column, but entities table uses `entity_project_association` junction table instead.
**Fix:** Insert entity, then INSERT into `entity_project_association(entity_id, project_id)`.

## SQLite Schema Notes

### Key Tables
- `entities` — has `user_id` (FK to users), `name`, `entity_type`, `tags` (JSON), no `project_ids` column
- `entity_project_association(entity_id, project_id)` — junction table for entity→project
- `entity_relationships` — has `user_id`, `source_entity_id`, `target_entity_id`, `relationship_type`
- `memory_entity_association(memory_id, entity_id)` — links memories to entities
- `memory_document_association(memory_id, document_id)` — links memories to documents
- `code_artifacts` — has `user_id`, `project_id`, `title`, `code`, `language`, `tags` (JSON)
- `documents` — has `user_id`, `project_id`, `title`, `content`, `document_type`, `size_bytes`
- `memory_project_association(memory_id, project_id)` — links memories to projects

### Default User
```
user_id = "ac1c407b-b658-4a44-b3ea-edc19fce87f1"
```

### Getting Next ID
```sql
SELECT COALESCE(MAX(id), 0) FROM <table>
```

## Lessons for Future Encodings

1. **Always use direct SQLite for entities/artifacts/documents** — MCP tools are unreliable
2. **Always fix project associations after encoding** — forgetful_save caches old project
3. **Use `entity_project_association` table** — not `project_ids` column on entities
4. **Verify memory count per project** at the end with `SELECT COUNT(*) FROM memory_project_association WHERE project_id = ?`
5. **Terminal tool is unreliable** — always prefer execute_code with Python

## Repo-Specific Notes (Ouroboros)
- Medium profile (253 files), only produced 22 memories — could expand further
- Has significant documentation in docs/ (authentication.md, proactive-notifications.md, activity_stream.md)
- Uses protocol-based repository pattern extensively
- MCP integration via Forgetful for semantic memory
- Multiple agent types with specialized capabilities
