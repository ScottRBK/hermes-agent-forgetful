# Forgetful Repo Encoder

You are encoding the repository at `{{REPO_PATH}}` into the Forgetful knowledge base. Profile: **{{PROFILE}}**. Project name (Forgetful): **{{PROJECT_NAME}}** (id={{PROJECT_ID}}).

Work through the phases below **in order**. After each phase, output a one-line completion report. Do **not** skip mandatory phases. Use whatever LLM model the user has configured — **do not switch models, do not pick reasoning levels**, just do the work.

## Tools you will use

You have:
- `Read`, `Glob`, `Grep` — to walk and inspect the repo.
- `forgetful_save` — create a memory (use `scope='current'` so it tags this project).
- `forgetful_link` — link related memories.
- `forgetful_recall` — check whether a memory already exists before re-creating.
- `execute_forgetful_tool` (if exposed) for entities/documents/code-artifacts that have no convenience wrapper. Specifically:
  - `create_entity({"name", "entity_type", "notes", "aka", "tags", "project_ids": [{{PROJECT_ID}}]})`
  - `create_entity_relationship({"source_entity_id", "target_entity_id", "relationship_type", "notes"})`
  - `link_entity_to_memory({"entity_id", "memory_id"})`
  - `create_code_artifact({"title", "description", "code", "language", "tags", "project_id": {{PROJECT_ID}}})`
  - `create_document({"title", "description", "content", "document_type", "project_id": {{PROJECT_ID}}})`
  - `update_project({"project_id": {{PROJECT_ID}}, "notes": "..."})`

If a tool argument feels uncertain, call `how_to_use_forgetful_tool({"tool_name": "<name>"})` first.

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
4. Run `forgetful_recall({"query": "{{PROJECT_NAME}} architecture", "scope": "current"})` to see what already exists. **Skip work that's already covered.**

Output: `Phase 0 complete — read N files, manifests=[...], existing memories=N`.

---

## Phase 1 — Foundation memories (mandatory, 3–12 memories per profile band)

Create the foundational memories that anyone joining this project should be able to recall in five seconds:

1. **What it is** — one-paragraph elevator pitch (importance 9).
2. **Why it exists** — the problem it solves and the audience it serves (importance 9).
3. **Top-level architecture** — the major components and how data flows between them (importance 9).
4. **How to run it locally** — install + run + test in shell commands (importance 8).
5. **Key technologies / runtime** — language, framework, runtime version, key libraries (importance 8).

Each memory: scope=`current`, importance ≥ 8, tags include `foundation`. Then update the project's notes:

```
update_project({"project_id": {{PROJECT_ID}}, "notes": "<2-3 sentence project primer for instant context>"})
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
4. For frameworks worth deeper docs, the agent calling this encoder later can use `forgetful_gather_context` with `frameworks=[...]` to pull Context7 details — **do not** pull Context7 yourself in this phase.

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
create_entity({
  "name": "<short readable name>",
  "entity_type": "Component",
  "notes": "<one-sentence description + path>",
  "tags": ["architecture"],
  "project_ids": [{{PROJECT_ID}}]
})
```

Then create relationships between them:
```
create_entity_relationship({
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

For each: `create_code_artifact({"title", "description", "code", "language", "tags": [...], "project_id": {{PROJECT_ID}}})`. Limit each artifact to ≤ 80 lines of code; truncate longer functions sensibly.

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
create_document({
  "title": "<doc title>",
  "description": "<one-paragraph what's inside>",
  "content": "<the full doc text>",
  "document_type": "text",
  "project_id": {{PROJECT_ID}}
})
```

For each document, create a one-paragraph memory that points at it (`document_ids=[doc_id]`) so semantic search surfaces the doc on related queries.

Output: `Phase 7 complete — N documents + N entry memories`.

---

## Phase 7B — Architecture reference document (mandatory)

Synthesise everything from Phase 2 + Phase 2B into a single Architecture Reference document:

```
create_document({
  "title": "{{PROJECT_NAME}} Architecture Reference",
  "description": "Synthesised architecture overview — components, data flow, key files.",
  "content": "<long-form synthesis: components, dataflow diagram in mermaid or ascii, key files, dependency surface>",
  "document_type": "architecture",
  "project_id": {{PROJECT_ID}}
})
```

Then create one entry memory at importance 9 with `document_ids=[doc_id]` so this is the first thing recall surfaces for "{{PROJECT_NAME}} architecture".

Output: `Phase 7B complete — architecture document id=N + entry memory id=M`.

---

## Final summary

After all phases, print:

```
Encoding complete for {{PROJECT_NAME}} ({{PROFILE}}).
- Memories created: N (target: <profile band>)
- Entities created: N
- Code artifacts: N
- Documents: N
- Phases run: 0, 1, [1B if applicable], 2, 2B, 3, [4 if applicable], [5 if applicable], 6, [7 if applicable], 7B
- Phases skipped: 6B (Serena LSP not integrated)
```

Then stop. Do **not** continue with unrelated work.
