---
name: encode-repo
description: Multi-phase repository encoder for the Forgetful knowledge base. Activate when the user asks to "encode this repo", "bootstrap forgetful for project X", "populate the knowledge base from this codebase", or runs `hermes forgetful encode`. Walks the repo through 8 phases (Foundation → Dependencies → Architecture → Entity Graph → Patterns → Features → Decisions → Code Artifacts → Architecture Document) using Read/Glob/Grep + the forgetful_* tools.
version: 1.0.0
metadata:
  hermes:
    tags: [forgetful, memory, encoder, knowledge-graph, bootstrap, repo]
    related_skills: []
---

# Encode Repository → Forgetful

You are encoding a codebase into the Forgetful knowledge base. Work through the phases below **in order**. After each phase, output a one-line completion report. Do **not** skip mandatory phases. Use whatever model the user has configured — **do not switch models, do not pick reasoning levels** — just do the work.

## Inputs you need

Before starting, establish:

1. **Repo path** — the absolute path to the codebase being encoded. If the user said "encode this repo" without specifying, use the current working directory. Confirm with the user before proceeding if ambiguous.
2. **Project profile** — `small` / `small_complex` / `medium` / `large`. If not specified, auto-detect from source-file count (excluding `.venv`, `node_modules`, `vendor`, `__pycache__`, `dist`, `build`, etc.):
   - `< 25` source files → `small`
   - `< 100` → `small_complex`
   - `< 500` → `medium`
   - `≥ 500` → `large`
3. **Active Forgetful project** — read `~/.hermes/forgetful.json` for `project_id` and `project_name`. The encoded memories need a project home, so:
   - If `~/.hermes/forgetful.json` does **not exist**, stop and tell the user to run `hermes memory setup` (the wizard creates the config and binds an initial project).
   - If the file exists but `project_id` / `project_name` are unset, stop and tell the user to run `hermes forgetful project switch <name>` (or `hermes forgetful project create <name>` if no project exists yet).

State all three back to the user as a one-line confirmation before starting Phase 0.

## Tools you will use

- `execute_code` (Python) — **Primary tool for file discovery.** Use `os.walk()` with absolute paths to scan repo structure, count source files, and read manifests. The `terminal` tool frequently gets stuck on a stale non-existent working directory (e.g. `/development`) causing all commands to fail with `FileNotFoundError`. Always prefer `execute_code` with absolute paths for reliability.
- `Read`, `Glob`, `Grep` — for targeted file reads and pattern searches (fallback if `execute_code` isn't suitable for the specific task).
- `forgetful_save` — create a memory (set `scope='current'` so writes are tagged with the active project).
- `forgetful_link` — link related memories.
- `forgetful_recall` — check whether a memory already exists before re-creating.
- `execute_forgetful_tool` — for entity / document / code-artifact operations not covered by the convenience tools:
  - `create_entity({"name", "entity_type", "notes", "aka", "tags", "project_ids": [<active project id>]})`
  - `create_entity_relationship({"source_entity_id", "target_entity_id", "relationship_type", "notes"})`
  - `link_entity_to_memory({"entity_id", "memory_id"})`
  - `create_code_artifact({"title", "description", "code", "language", "tags", "project_id": <active project id>})`
  - `create_document({"title", "description", "content", "document_type", "project_id": <active project id>})`
  - `update_project({"project_id": <active project id>, "notes": "..."})`

If a tool argument feels uncertain, call `how_to_use_forgetful_tool({"tool_name": "<name>"})` first. Don't invent kwargs.

## Profile targets (encoding budget)

| Profile | Memories | Entities | Code Artifacts | Documents |
|---|---|---|---|---|
| small | 17–31 | 3–5 | 3–5 | 1–2 |
| small_complex | 28–46 | 5–8 | 4–7 | 2–3 |
| medium | 38–66 | 8–15 | 5–10 | 3–5 |
| large | 66–112 | 15–25 | 8–15 | 5–10 |

If your profile is unknown, default to **medium**. Stay within the band — don't chase artificial counts.

---

## Phase 0 — Discovery (mandatory)

1. List the top-level directory contents (`Glob` for `*` at depth 1, then `*/` for subdirs).
2. `Read` the README (or top-level docs file) — record the elevator pitch.
3. Detect manifest files: `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Gemfile`, etc.
4. Run `forgetful_recall({"query": "<project name> architecture", "scope": "current"})` to see what already exists. **Skip work that's already covered.**

Output: `Phase 0 complete — read N files, manifests=[...], existing memories=N`.

---

## Phase 1 — Foundation memories (mandatory, 3–12 memories per profile band)

Create the foundational memories that anyone joining this project should be able to recall in five seconds:

1. **What it is** — one-paragraph elevator pitch (importance 9).
2. **Why it exists** — the problem it solves and the audience it serves (importance 9).
3. **Top-level architecture** — the major components and how data flows between them (importance 9).
4. **How to run it locally** — install + run + test in shell commands (importance 8).
5. **Key technologies / runtime** — language, framework, runtime version, key libraries (importance 8).

Each memory: `scope='current'`, importance ≥ 8, tags include `foundation`. Then update the project's notes:

```
execute_forgetful_tool("update_project", {
  "project_id": <active project id>,
  "notes": "<2-3 sentence project primer for instant context>"
})
```

Output: `Phase 1 complete — created N foundation memories`.

---

## Phase 1B — Dependency memories (conditional, 1–3 memories)

Skip if the project has no manifest. Otherwise:

1. Parse the dependency manifest(s).
2. Group dependencies by purpose (web framework, db driver, testing, build, etc.).
3. Create one memory per major framework with **importance 7**, including:
   - Library name + version pinned
   - Why this library is used in this project
   - Any constraints (version pins, custom forks, replacements)
4. For frameworks worth deeper docs, the user can later use `forgetful_gather_context` with `frameworks=[...]` to pull Context7 details — **do not** pull Context7 yourself in this phase.

Output: `Phase 1B complete — created N dependency memories`.

---

## Phase 2 — Architecture (mandatory, profile-band memories)

For the medium/large profiles especially, walk through the major source directories:

1. `Glob` for source files (`**/*.py`, `**/*.ts`, etc.).
2. `Grep` for class/function definitions in 5–15 files that look architecturally important (entry points, framework hooks, top-level modules).
3. For each subsystem, create a memory describing:
   - What it does (single sentence).
   - Where it lives (file paths).
   - Public interfaces it exposes (functions/classes/HTTP routes).
   - What it depends on (other subsystems, libraries).

Use `forgetful_link` to connect each architecture memory to the matching foundation memory.

Output: `Phase 2 complete — created N architecture memories, linked to foundation`.

---

## Phase 2B — Entity Graph (mandatory, profile-band entities)

Entities make the knowledge graph navigable. Create entities for:

- **Components** (one per subsystem from Phase 2). Type = `Component`.
- **External services** (databases, message brokers, third-party APIs). Type = `Service`.
- **Key files** for very large modules. Type = `File`.

For each entity:
```
execute_forgetful_tool("create_entity", {
  "name": "<short readable name>",
  "entity_type": "Component",
  "notes": "<one-sentence description + path>",
  "tags": ["architecture"],
  "project_ids": [<active project id>]
})
```

Then create relationships between them:
```
execute_forgetful_tool("create_entity_relationship", {
  "source_entity_id": <id>,
  "target_entity_id": <id>,
  "relationship_type": "depends_on" | "uses" | "exposes" | "managed_by",
  "notes": "<why>"
})
```

Finally, `link_entity_to_memory` each entity to the architecture memory(ies) that describe it. Minimum 3 entities; otherwise the graph isn't useful.

Output: `Phase 2B complete — N entities, M relationships, all linked to memories`.

---

## Phase 3 — Patterns (mandatory, minimum 3 pattern memories)

Identify patterns the project uses repeatedly. `Grep` for:
- Decorator names that recur (`@app.route`, `@pytest.fixture`, etc.).
- Common helper or middleware names.
- Error-handling shapes (custom exceptions, error wrappers).
- Test fixtures and mocks.

For each pattern, create a memory at importance 7:
- The pattern name.
- One concrete example with a code snippet (≤ 30 lines).
- When to use it / when NOT to use it.

Tag with `pattern`. If a pattern has a canonical implementation file, link to that file's architecture memory.

Output: `Phase 3 complete — created N pattern memories`.

---

## Phase 4 — Critical features (conditional, 2–10 memories)

If the project has identifiable user-facing features (endpoints, CLI commands, jobs), pick the 3–10 most important and document each:

- What the user can do.
- Where it's implemented (entry-point file).
- Test file (if any).
- Edge cases / known gotchas (mine from comments, TODOs, recent commit messages via `git log`).

Importance 7. Tag with `feature`. Skip this phase if the project doesn't have user-facing features (e.g., a pure library).

Output: `Phase 4 complete — N feature memories` or `Phase 4 skipped — no user-facing features`.

---

## Phase 5 — Decisions (conditional — DOCUMENTATION-ONLY)

**Only run this phase if the project has explicit documentation of design decisions** — ADRs, RFCs, design docs in `docs/`, README sections titled "Why", commit messages tagged `[design]`, etc. **Do not invent decisions.**

For each documented decision:
- Title: `Decision: <short summary>`
- Content: the decision + the alternatives considered + the chosen rationale.
- Source: cite the file/line/PR.
- Importance 8, tag `decision`.

Output: `Phase 5 complete — N decision memories from sources [files...]` or `Phase 5 skipped — no documented decisions found`.

---

## Phase 6 — Code artifacts (mandatory, minimum 3)

Code artifacts are reusable snippets — not whole files, not tests. Find 3–15 (per profile band) snippets that someone joining the project would copy as a starting point:

- A canonical handler/middleware.
- A representative test fixture.
- A typical migration / Alembic revision shape.
- A standard configuration block.
- A non-trivial reusable utility function.

For each: `create_code_artifact({"title", "description", "code", "language", "tags": [...], "project_id": <active project id>})`. Limit each artifact to ≤ 80 lines of code; truncate longer functions sensibly.

Then create a memory linking back to the artifact (`code_artifact_ids=[...]` on the memory). The memory describes WHY this snippet matters; the artifact holds the code.

Output: `Phase 6 complete — N code artifacts + linking memories`.

---

## Phase 6B — Symbol index — SKIPPED in this v1 encoder

This phase requires a language server (Serena) which the hermes plugin doesn't depend on. Skip and continue.

Output: `Phase 6B skipped — no LSP integration`.

---

## Phase 7 — Additional documents (conditional, 0–10 docs)

If the project has long-form content worth surfacing — `docs/architecture.md`, RFCs in `docs/rfcs/`, runbook entries — extract them as Forgetful documents:

```
execute_forgetful_tool("create_document", {
  "title": "<doc title>",
  "description": "<one-paragraph what's inside>",
  "content": "<the full doc text>",
  "document_type": "text",
  "project_id": <active project id>
})
```

For each document, create a one-paragraph memory that points at it (`document_ids=[doc_id]`) so semantic search surfaces the doc on related queries.

Output: `Phase 7 complete — N documents + N entry memories`.

---

## Phase 7B — Architecture reference document (mandatory)

Synthesise everything from Phase 2 + Phase 2B into a single Architecture Reference document:

```
execute_forgetful_tool("create_document", {
  "title": "<project name> Architecture Reference",
  "description": "Synthesised architecture overview — components, data flow, key files.",
  "content": "<long-form synthesis: components, dataflow diagram in mermaid or ascii, key files, dependency surface>",
  "document_type": "architecture",
  "project_id": <active project id>
})
```

Then create one entry memory at importance 9 with `document_ids=[doc_id]` so this is the first thing recall surfaces for "<project name> architecture".

Output: `Phase 7B complete — architecture document id=N + entry memory id=M`.

---

## Pitfalls

### Terminal tool stale working directory
The `terminal` tool can get stuck on a non-existent working directory (e.g. `/development`) causing every command to fail with `FileNotFoundError: [Errno 2] No such file or directory: '/development'`. This loop is hard to break — even `cd` commands fail. **Always use `execute_code` with Python and absolute paths** for file discovery, manifest parsing, and source file counting. The terminal is unreliable for initial repo exploration.

### Profile detection excludes noise dirs
When counting source files for profile detection, exclude: `.venv`, `node_modules`, `vendor`, `__pycache__`, `dist`, `build`, `.git`, `.ruff_cache`, `.pytest_cache`, `.uv-cache`, `.serena`, `.claude`, `target`. Count only meaningful source files (`.py`, `.ts`, `.js`, `.rs`, `.go`, `.java`, `.rb`, `.md`, `.toml`, `.yaml`, `.yml`, `.json`, `.ini`, `.sql`, `.mako`, `.sh`).

### Active project mismatch
The Forgetful active project may not match the repo being encoded. Always confirm with the user whether to encode into the current project or switch/create a dedicated one before proceeding. Example: encoding the Forgetful source repo into a "plugins" project is likely wrong — the user probably wants a "forgetful" project.

---

## Final summary

After all phases, print:

```
Encoding complete for <project name> (<profile>).
- Memories created: N (target: <profile band>)
- Entities created: N
- Code artifacts: N
- Documents: N
- Phases run: 0, 1, [1B if applicable], 2, 2B, 3, [4 if applicable], [5 if applicable], 6, [7 if applicable], 7B
- Phases skipped: 6B (Serena LSP not integrated)
```

Then stop. Do **not** continue with unrelated work.
