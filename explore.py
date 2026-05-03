"""5-phase deep graph traversal of the Forgetful knowledge base.

Ports the exploring-knowledge-graph skill contract from context-hub-plugin
(skills/exploring-knowledge-graph/SKILL.md), implemented as pure Python
orchestration in the plugin process. Returns structured markdown — the
agent calling the tool synthesizes from there.

Phases:
  1. Semantic entry point — query_memory(topic, include_links=True)
  2. Expand memory details — get_memory for primaries (harvest project_ids,
     code_artifact_ids, document_ids, linked_memory_ids)
  3. Entity discovery — list_entities scoped to discovered project_ids
  4. Entity relationships — get_entity_relationships(direction='both')
  5. Entity-linked memories — get_entity_memories per entity

Depth:
  shallow  → phases 1-2
  medium   → phases 1-4 (default)
  deep     → all five phases
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from .client import ForgetfulClient, ForgetfulClientError
from .config import ForgetfulConfig

logger = logging.getLogger(__name__)


_DEFAULTS_BY_DEPTH = {
    "shallow": {"k": 5, "max_links": 3, "max_entities": 0, "max_entity_memories": 0},
    "medium":  {"k": 10, "max_links": 5, "max_entities": 8, "max_entity_memories": 0},
    "deep":    {"k": 15, "max_links": 8, "max_entities": 15, "max_entity_memories": 5},
}


def run_explore(
    *,
    client: ForgetfulClient,
    config: ForgetfulConfig,
    topic: str,
    depth: str = "medium",
) -> str:
    """Execute the phased exploration and return a markdown report."""
    topic = (topic or "").strip()
    if not topic:
        return "_(empty topic)_"

    depth_key = depth if depth in _DEFAULTS_BY_DEPTH else "medium"
    knobs = _DEFAULTS_BY_DEPTH[depth_key]

    state = _ExploreState(topic=topic, depth=depth_key)

    # --- Phase 1: semantic entry ---
    _phase_semantic_entry(client, state, k=knobs["k"], max_links=knobs["max_links"])

    # --- Phase 2: expand memory details ---
    _phase_expand_memories(client, state)

    if depth_key == "shallow":
        return _render(state)

    # --- Phase 3: entity discovery ---
    _phase_entity_discovery(client, state, max_entities=knobs["max_entities"])

    # --- Phase 4: entity relationships ---
    _phase_entity_relationships(client, state)

    if depth_key == "medium":
        return _render(state)

    # --- Phase 5: entity-linked memories ---
    _phase_entity_memories(client, state, per_entity=knobs["max_entity_memories"])

    return _render(state)


# ---------------------------------------------------------------------------
# State container
# ---------------------------------------------------------------------------

class _ExploreState:
    def __init__(self, *, topic: str, depth: str) -> None:
        self.topic = topic
        self.depth = depth
        self.primary_memories: List[Dict[str, Any]] = []
        self.linked_memories: Dict[int, Dict[str, Any]] = {}
        self.entity_linked_memories: Dict[int, Dict[str, Any]] = {}
        self.expanded_memories: Dict[int, Dict[str, Any]] = {}
        self.discovered_project_ids: Set[int] = set()
        self.entities: List[Dict[str, Any]] = []
        self.relationships: Dict[int, List[Dict[str, Any]]] = {}
        self.errors: List[str] = []


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def _phase_semantic_entry(
    client: ForgetfulClient, state: _ExploreState, *, k: int, max_links: int,
) -> None:
    payload = {
        "query": state.topic,
        "query_context": f"Knowledge-graph exploration ({state.depth} depth).",
        "k": k,
        "include_links": True,
        "max_links_per_primary": max_links,
    }
    try:
        result = client.execute(
            "execute_forgetful_tool",
            {"tool_name": "query_memory", "arguments": payload},
        )
    except ForgetfulClientError as exc:
        state.errors.append(f"phase 1 (semantic entry) failed: {exc}")
        return
    state.primary_memories = [
        m for m in (result.get("primary_memories") or []) if isinstance(m, dict)
    ]
    for m in result.get("linked_memories") or []:
        if not isinstance(m, dict):
            continue
        mid = _coerce_int(m.get("id"))
        if mid is not None:
            state.linked_memories[mid] = m
    for m in state.primary_memories:
        for pid in m.get("project_ids") or []:
            pid_i = _coerce_int(pid)
            if pid_i is not None:
                state.discovered_project_ids.add(pid_i)


def _phase_expand_memories(client: ForgetfulClient, state: _ExploreState) -> None:
    """Pull full memory records for primaries we haven't already expanded."""
    for memory in state.primary_memories[:8]:  # bounded
        mid = _coerce_int(memory.get("id"))
        if mid is None or mid in state.expanded_memories:
            continue
        try:
            res = client.execute(
                "execute_forgetful_tool",
                {"tool_name": "get_memory", "arguments": {"memory_id": mid}},
            )
        except ForgetfulClientError as exc:
            state.errors.append(f"phase 2 get_memory({mid}) failed: {exc}")
            continue
        full = _unwrap_single(res)
        if isinstance(full, dict):
            state.expanded_memories[mid] = full
            for pid in full.get("project_ids") or []:
                pid_i = _coerce_int(pid)
                if pid_i is not None:
                    state.discovered_project_ids.add(pid_i)


def _phase_entity_discovery(
    client: ForgetfulClient, state: _ExploreState, *, max_entities: int,
) -> None:
    """Surface entities from discovered projects, with a fallback for unscoped ones.

    Forgetful entities can have an empty ``project_ids`` (cross-project /
    unscoped — common for people-as-entities). The skill contract says
    "find entities in discovered projects", but a strict project filter
    would hide these. So:

      1. Try project-filtered list first when projects were discovered.
      2. If that returns empty (or no projects), fall back to a global
         list capped at ``max_entities``.
    """
    if max_entities == 0:
        return

    collected: List[Dict[str, Any]] = []
    project_ids = sorted(state.discovered_project_ids)

    if project_ids:
        try:
            res = client.execute(
                "execute_forgetful_tool",
                {"tool_name": "list_entities", "arguments": {"project_ids": project_ids}},
            )
        except ForgetfulClientError as exc:
            state.errors.append(f"phase 3 list_entities (scoped) failed: {exc}")
        else:
            collected.extend(_extract_entity_list(res))

    if not collected:
        try:
            res = client.execute(
                "execute_forgetful_tool",
                {"tool_name": "list_entities", "arguments": {}},
            )
        except ForgetfulClientError as exc:
            state.errors.append(f"phase 3 list_entities (global) failed: {exc}")
            return
        collected.extend(_extract_entity_list(res))

    state.entities = collected[:max_entities]


def _extract_entity_list(response: Any) -> List[Dict[str, Any]]:
    """Pull a list of entity dicts from any of the response shapes."""
    payload = _unwrap_single(response)
    if isinstance(payload, dict):
        payload = payload.get("entities") or payload.get("results") or []
    if not isinstance(payload, list):
        return []
    return [e for e in payload if isinstance(e, dict)]


def _phase_entity_relationships(
    client: ForgetfulClient, state: _ExploreState,
) -> None:
    if not state.entities:
        return
    for entity in state.entities:
        eid = _coerce_int(entity.get("id"))
        if eid is None:
            continue
        try:
            res = client.execute(
                "execute_forgetful_tool",
                {
                    "tool_name": "get_entity_relationships",
                    "arguments": {"entity_id": eid, "direction": "both"},
                },
            )
        except ForgetfulClientError as exc:
            state.errors.append(f"phase 4 get_entity_relationships({eid}) failed: {exc}")
            continue
        rels = _unwrap_single(res)
        if isinstance(rels, dict):
            rels = rels.get("relationships") or rels.get("results") or []
        if isinstance(rels, list):
            state.relationships[eid] = [r for r in rels if isinstance(r, dict)]


def _phase_entity_memories(
    client: ForgetfulClient, state: _ExploreState, *, per_entity: int,
) -> None:
    if per_entity <= 0 or not state.entities:
        return
    visited = set(state.expanded_memories.keys()) | set(state.linked_memories.keys())
    visited.update(_coerce_int(m.get("id")) for m in state.primary_memories if m.get("id"))

    for entity in state.entities:
        eid = _coerce_int(entity.get("id"))
        if eid is None:
            continue
        try:
            res = client.execute(
                "execute_forgetful_tool",
                {"tool_name": "get_entity_memories", "arguments": {"entity_id": eid}},
            )
        except ForgetfulClientError as exc:
            state.errors.append(f"phase 5 get_entity_memories({eid}) failed: {exc}")
            continue
        payload = _unwrap_single(res)
        if isinstance(payload, dict):
            mem_ids = payload.get("memory_ids") or []
        elif isinstance(payload, list):
            mem_ids = payload
        else:
            mem_ids = []

        new_ids = [mid for mid in mem_ids if _coerce_int(mid) is not None][:per_entity]
        for raw_id in new_ids:
            mid = _coerce_int(raw_id)
            if mid is None or mid in visited:
                continue
            visited.add(mid)
            try:
                fetched = client.execute(
                    "execute_forgetful_tool",
                    {"tool_name": "get_memory", "arguments": {"memory_id": mid}},
                )
            except ForgetfulClientError as exc:
                state.errors.append(f"phase 5 get_memory({mid}) failed: {exc}")
                continue
            full = _unwrap_single(fetched)
            if isinstance(full, dict):
                state.entity_linked_memories[mid] = full


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _render(state: _ExploreState) -> str:
    out: List[str] = [
        f"# Knowledge Graph Exploration: {state.topic}",
        f"_Depth: {state.depth}_",
        "",
    ]

    out.append("## Memories")
    out.append("")

    out.append("### Primary (direct matches)")
    if not state.primary_memories:
        out.append("_no matches_")
    else:
        for m in state.primary_memories:
            out.append(_render_memory_line(m))
    out.append("")

    out.append("### Linked (1-hop via embedding similarity)")
    if not state.linked_memories:
        out.append("_no linked memories surfaced_")
    else:
        for m in state.linked_memories.values():
            out.append(_render_memory_line(m))
    out.append("")

    if state.entity_linked_memories:
        out.append("### Entity-linked (discovered via entity-memory edges)")
        for m in state.entity_linked_memories.values():
            out.append(_render_memory_line(m))
        out.append("")

    out.append("## Entities")
    out.append("")
    if not state.entities:
        if state.depth == "shallow":
            out.append("_(skipped — shallow depth)_")
        else:
            out.append("_no entities found in discovered projects_")
    else:
        for e in state.entities:
            eid = e.get("id")
            name = (e.get("name") or "(unnamed)").strip()
            etype = (e.get("entity_type") or e.get("type") or "?").strip()
            rel_count = len(state.relationships.get(_coerce_int(eid) or -1, []))
            notes = (e.get("notes") or "").strip()
            out.append(f"- **{name}** *(type={etype}, id={eid}, relationships={rel_count})*")
            if notes:
                snippet = notes if len(notes) <= 200 else notes[:197] + "…"
                out.append(f"    {snippet}")
    out.append("")

    if state.relationships:
        out.append("## Relationships")
        out.append("")
        for eid, rels in state.relationships.items():
            entity = next((e for e in state.entities if _coerce_int(e.get("id")) == eid), None)
            anchor = (entity.get("name") if entity else f"entity {eid}") or f"entity {eid}"
            out.append(f"### {anchor} (id={eid})")
            if not rels:
                out.append("_no relationships_")
                continue
            for r in rels[:10]:
                rtype = r.get("relationship_type") or r.get("type") or "?"
                src = r.get("source_entity_name") or r.get("source_id") or "?"
                dst = r.get("target_entity_name") or r.get("target_id") or "?"
                out.append(f"- {src} —[{rtype}]→ {dst}")
            out.append("")

    out.append("## Graph Summary")
    out.append("")
    total_memories = (
        len(state.primary_memories)
        + len(state.linked_memories)
        + len(state.entity_linked_memories)
    )
    total_relationships = sum(len(v) for v in state.relationships.values())
    out.append(f"- Memories surfaced: **{total_memories}** "
               f"(primary={len(state.primary_memories)}, "
               f"linked={len(state.linked_memories)}, "
               f"entity-linked={len(state.entity_linked_memories)})")
    out.append(f"- Entities surfaced: **{len(state.entities)}**")
    out.append(f"- Relationships traversed: **{total_relationships}**")
    out.append(f"- Projects touched: **{len(state.discovered_project_ids)}** "
               f"({sorted(state.discovered_project_ids)})")
    if state.errors:
        out.append("")
        out.append("### Phase errors")
        for err in state.errors:
            out.append(f"- {err}")

    return "\n".join(out).rstrip() + "\n"


def _render_memory_line(memory: Dict[str, Any]) -> str:
    title = (memory.get("title") or "(untitled)").strip()
    importance = memory.get("importance")
    mid = memory.get("id")
    project_ids = memory.get("project_ids") or []
    snippet = (memory.get("content") or "").strip()
    if snippet:
        snippet = snippet if len(snippet) <= 180 else snippet[:177] + "…"
    line = f"- **{title}** *(id={mid}, importance={importance}/10, projects={project_ids})*"
    if snippet:
        line += f"\n    {snippet}"
    return line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unwrap_single(response: Any) -> Any:
    if isinstance(response, dict) and set(response.keys()) == {"result"}:
        return response["result"]
    return response
