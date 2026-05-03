"""Multi-source context-gather orchestration.

Ports the four-source strategy from context-hub-plugin's context-retrieval
agent (https://github.com/ScottRBK/context-hub-plugin), adapted for hermes
constraints:

- No subagent spawn / no model selection — the plugin is provider-agnostic.
  We assemble RAW context (memories, linked artifacts, framework docs) and
  return it as structured markdown. The agent that called the tool does
  the synthesis using whatever model the user has configured.
- Cross-project recall by default — preserves "we already solved this"
  pattern from the original agent.
- Context7 lookup is best-effort; runs only when frameworks are passed in
  AND the network call succeeds. Failures are logged and elided from the
  output, never raised.

Output contract (six sections, mirrored from context-retrieval.md):
  # Context for: <task>
  ## Relevant Memories
  ## Code Patterns & Snippets
  ## Framework-Specific Guidance (if applicable)
  ## Architectural Decisions to Consider
  ## Knowledge Graph Insights
  ## Implementation Notes
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .client import ForgetfulClient, ForgetfulClientError
from .config import ForgetfulConfig

logger = logging.getLogger(__name__)


_RECALL_K = 10
_MAX_LINKED_ARTIFACTS = 5
_MAX_LINKED_DOCS = 3
_CONTEXT7_BASE = "https://context7.com/api/v1"
_CONTEXT7_TIMEOUT = 8.0
_CONTEXT7_DOCS_CHAR_BUDGET = 4000


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_gather(
    *,
    client: ForgetfulClient,
    config: ForgetfulConfig,
    task: str,
    frameworks: Optional[List[str]] = None,
    include_web: bool = False,
) -> str:
    """Gather context across Forgetful + optional Context7, return markdown.

    The caller is responsible for the ForgetfulClient lifecycle; this
    function only consumes it via ``client.execute(...)``.
    """
    task = (task or "").strip()
    if not task:
        return "_(empty task)_"

    frameworks = [f for f in (frameworks or []) if isinstance(f, str) and f.strip()]

    # Phase 1 — Forgetful semantic recall (cross-project)
    primaries, linked_index = _query_forgetful(client, task)

    # Phase 2 — resolve linked code artifacts and documents
    code_artifacts = _resolve_code_artifacts(client, primaries, linked_index)
    documents = _resolve_documents(client, primaries, linked_index)

    # Phase 3 — optional Context7 framework lookup
    framework_blocks = _query_context7(frameworks) if frameworks else []

    # include_web is reserved for future WebSearch integration. v1 omits.
    # Skipped silently — documented in README.
    _ = include_web

    return _render_report(
        task=task,
        primaries=primaries,
        linked_index=linked_index,
        code_artifacts=code_artifacts,
        documents=documents,
        framework_blocks=framework_blocks,
    )


# ---------------------------------------------------------------------------
# Phase 1 — Forgetful recall
# ---------------------------------------------------------------------------

def _query_forgetful(
    client: ForgetfulClient, task: str,
) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    """Return (primaries, linked_index) — cross-project recall."""
    payload = {
        "query": task,
        "query_context": (
            "Context-gather request: assembling implementation context "
            "before planning or coding. Cross-project recall preferred."
        ),
        "k": _RECALL_K,
        "include_links": True,
        "max_links_per_primary": 3,
    }
    try:
        result = client.execute(
            "execute_forgetful_tool",
            {"tool_name": "query_memory", "arguments": payload},
        )
    except ForgetfulClientError as exc:
        logger.warning("forgetful: gather query_memory failed: %s", exc)
        return [], {}

    primaries = [
        m for m in (result.get("primary_memories") or []) if isinstance(m, dict)
    ]
    linked_index: Dict[int, Dict[str, Any]] = {}
    for entry in result.get("linked_memories") or []:
        if not isinstance(entry, dict):
            continue
        mid = _coerce_int(entry.get("id"))
        if mid is not None:
            linked_index[mid] = entry
    return primaries, linked_index


# ---------------------------------------------------------------------------
# Phase 2 — resolve linked artifacts & documents
# ---------------------------------------------------------------------------

def _resolve_code_artifacts(
    client: ForgetfulClient,
    primaries: List[Dict[str, Any]],
    linked_index: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Fetch up to N unique code_artifacts referenced by recalled memories."""
    artifact_ids: List[int] = []
    seen: set[int] = set()
    for memory in primaries + list(linked_index.values()):
        for raw_id in memory.get("code_artifact_ids") or []:
            aid = _coerce_int(raw_id)
            if aid is not None and aid not in seen:
                seen.add(aid)
                artifact_ids.append(aid)
                if len(artifact_ids) >= _MAX_LINKED_ARTIFACTS:
                    break
        if len(artifact_ids) >= _MAX_LINKED_ARTIFACTS:
            break

    fetched: List[Dict[str, Any]] = []
    for aid in artifact_ids:
        try:
            res = client.execute(
                "execute_forgetful_tool",
                {"tool_name": "get_code_artifact", "arguments": {"artifact_id": aid}},
            )
        except ForgetfulClientError as exc:
            logger.debug("forgetful: get_code_artifact(%s) failed: %s", aid, exc)
            continue
        artifact = _unwrap_single(res)
        if isinstance(artifact, dict):
            fetched.append(artifact)
    return fetched


def _resolve_documents(
    client: ForgetfulClient,
    primaries: List[Dict[str, Any]],
    linked_index: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Fetch up to N unique documents referenced by recalled memories."""
    doc_ids: List[int] = []
    seen: set[int] = set()
    for memory in primaries + list(linked_index.values()):
        for raw_id in memory.get("document_ids") or []:
            did = _coerce_int(raw_id)
            if did is not None and did not in seen:
                seen.add(did)
                doc_ids.append(did)
                if len(doc_ids) >= _MAX_LINKED_DOCS:
                    break
        if len(doc_ids) >= _MAX_LINKED_DOCS:
            break

    fetched: List[Dict[str, Any]] = []
    for did in doc_ids:
        try:
            res = client.execute(
                "execute_forgetful_tool",
                {"tool_name": "get_document", "arguments": {"document_id": did}},
            )
        except ForgetfulClientError as exc:
            logger.debug("forgetful: get_document(%s) failed: %s", did, exc)
            continue
        doc = _unwrap_single(res)
        if isinstance(doc, dict):
            fetched.append(doc)
    return fetched


# ---------------------------------------------------------------------------
# Phase 3 — Context7
# ---------------------------------------------------------------------------

def _query_context7(frameworks: List[str]) -> List[Dict[str, str]]:
    """Best-effort Context7 lookup; empty list when unavailable.

    Doesn't require an API key for unauthenticated reads. When
    ``CONTEXT7_API_KEY`` is set, it's sent as a Bearer token to lift any
    rate limits.
    """
    try:
        import urllib.request
        import urllib.parse
        import os
    except ImportError:
        return []

    api_key = (os.environ.get("CONTEXT7_API_KEY") or "").strip()

    blocks: List[Dict[str, str]] = []
    for framework in frameworks[:5]:
        try:
            search = _http_get_json(
                f"{_CONTEXT7_BASE}/search?query={urllib.parse.quote(framework)}",
                api_key=api_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("context7 search(%s) failed: %s", framework, exc)
            continue

        results = (search or {}).get("results") or []
        if not isinstance(results, list) or not results:
            continue
        top = results[0] if isinstance(results[0], dict) else None
        if not top:
            continue
        lib_id = top.get("id") or ""
        if not lib_id:
            continue

        try:
            docs = _http_get_text(
                f"{_CONTEXT7_BASE}{lib_id}",
                api_key=api_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("context7 fetch(%s) failed: %s", lib_id, exc)
            continue

        snippet = (docs or "").strip()
        if len(snippet) > _CONTEXT7_DOCS_CHAR_BUDGET:
            snippet = snippet[:_CONTEXT7_DOCS_CHAR_BUDGET].rstrip() + "\n…(truncated)"

        blocks.append({
            "framework": framework,
            "title": top.get("title") or framework,
            "description": top.get("description") or "",
            "library_id": lib_id,
            "trust_score": str(top.get("trustScore") or ""),
            "snippet": snippet,
        })
    return blocks


def _http_get_json(url: str, *, api_key: str = "") -> Optional[Dict[str, Any]]:
    import urllib.request
    req = urllib.request.Request(url, headers=_context7_headers(api_key))
    with urllib.request.urlopen(req, timeout=_CONTEXT7_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_text(url: str, *, api_key: str = "") -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=_context7_headers(api_key))
    with urllib.request.urlopen(req, timeout=_CONTEXT7_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _context7_headers(api_key: str) -> Dict[str, str]:
    headers = {"User-Agent": "hermes-forgetful-plugin/0.1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


# ---------------------------------------------------------------------------
# Render — six-section markdown
# ---------------------------------------------------------------------------

def _render_report(
    *,
    task: str,
    primaries: List[Dict[str, Any]],
    linked_index: Dict[int, Dict[str, Any]],
    code_artifacts: List[Dict[str, Any]],
    documents: List[Dict[str, Any]],
    framework_blocks: List[Dict[str, str]],
) -> str:
    out: List[str] = [f"# Context for: {task}", ""]

    # 1. Relevant Memories
    out.append("## Relevant Memories")
    out.append("")
    if not primaries:
        out.append("_No matching memories found in the knowledge base._")
        out.append("")
    else:
        for memory in primaries:
            mid = memory.get("id")
            title = (memory.get("title") or "(untitled)").strip()
            importance = memory.get("importance", "?")
            project_ids = memory.get("project_ids") or []
            content = (memory.get("content") or "").strip()
            context = (memory.get("context") or "").strip()

            out.append(
                f"### {title}  *(id={mid}, importance={importance}/10, projects={project_ids})*"
            )
            if content:
                out.append(content)
            if context:
                out.append("")
                out.append(f"**Why it matters:** {context}")

            link_ids = memory.get("linked_memory_ids") or []
            if link_ids:
                names = []
                for lid in link_ids[:5]:
                    link = linked_index.get(_coerce_int(lid) or -1)
                    if link:
                        names.append(f"`{(link.get('title') or '?')[:60]}` (id={link.get('id')})")
                if names:
                    out.append("")
                    out.append(f"**Linked memories:** {', '.join(names)}")
            out.append("")

    # 2. Code Patterns & Snippets
    out.append("## Code Patterns & Snippets")
    out.append("")
    if not code_artifacts:
        out.append("_No code artifacts linked to recalled memories._")
        out.append("")
    else:
        for art in code_artifacts:
            title = (art.get("title") or "(untitled)").strip()
            language = (art.get("language") or "").strip()
            description = (art.get("description") or "").strip()
            code = (art.get("code") or "").strip()
            aid = art.get("id")
            out.append(f"### {title}  *(code_artifact id={aid})*")
            if description:
                out.append(description)
                out.append("")
            if code:
                # Cap at 80 lines of code, ~3000 chars to keep budget sane
                code_lines = code.splitlines()
                if len(code_lines) > 80:
                    code = "\n".join(code_lines[:80]) + "\n# …(truncated)"
                if len(code) > 3000:
                    code = code[:3000].rstrip() + "\n# …(truncated)"
                fence = language or ""
                out.append(f"```{fence}")
                out.append(code)
                out.append("```")
            out.append("")

    # 3. Framework-Specific Guidance
    out.append("## Framework-Specific Guidance")
    out.append("")
    if not framework_blocks:
        out.append("_No frameworks specified or Context7 lookup skipped._")
        out.append("")
    else:
        for block in framework_blocks:
            out.append(
                f"### {block['title']}  *(context7 library `{block['library_id']}`, trust {block['trust_score'] or '?'})*"
            )
            if block.get("description"):
                out.append(block["description"])
                out.append("")
            if block.get("snippet"):
                out.append(block["snippet"])
            out.append("")

    # 4. Architectural Decisions
    out.append("## Architectural Decisions to Consider")
    out.append("")
    decision_lines = _extract_decisions(primaries, linked_index)
    if decision_lines:
        out.extend(decision_lines)
    else:
        out.append("_No high-importance decision memories surfaced for this task._")
    out.append("")

    # 5. Knowledge Graph Insights
    out.append("## Knowledge Graph Insights")
    out.append("")
    if not linked_index:
        out.append("_No linked memories were traversed for this query._")
    else:
        unique_projects = sorted({
            p for memory in primaries for p in (memory.get("project_ids") or [])
        })
        out.append(
            f"- Surfaced across {len(unique_projects)} project(s) "
            f"({unique_projects}) — broad cross-project pattern."
            if len(unique_projects) > 1
            else f"- Concentrated in project(s) {unique_projects}."
        )
        out.append(f"- Walked {len(linked_index)} linked memory record(s) "
                   f"to assemble this report.")
        if documents:
            out.append(f"- Pulled in {len(documents)} linked document(s); "
                       f"see Implementation Notes for highlights.")
    out.append("")

    # 6. Implementation Notes
    out.append("## Implementation Notes")
    out.append("")
    if not documents:
        out.append("_No linked documents to surface._")
    else:
        for doc in documents:
            title = (doc.get("title") or "(untitled doc)").strip()
            doc_id = doc.get("id")
            description = (doc.get("description") or "").strip()
            content = (doc.get("content") or "").strip()
            if len(content) > 1500:
                content = content[:1500].rstrip() + "\n…(truncated — open document for full text)"
            out.append(f"### {title}  *(document id={doc_id})*")
            if description:
                out.append(description)
                out.append("")
            if content:
                out.append(content)
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def _extract_decisions(
    primaries: List[Dict[str, Any]],
    linked_index: Dict[int, Dict[str, Any]],
) -> List[str]:
    """Derive bullet-list architectural decisions from high-importance memories.

    Heuristic: importance ≥ 8 OR a 'decision' tag → one bullet per memory.
    """
    bullets: List[str] = []
    for memory in primaries + list(linked_index.values()):
        importance = _coerce_int(memory.get("importance")) or 0
        tags = [t.lower() for t in memory.get("tags") or [] if isinstance(t, str)]
        if importance < 8 and "decision" not in tags:
            continue
        title = (memory.get("title") or "").strip()
        context = (memory.get("context") or "").strip()
        snippet = context or (memory.get("content") or "").strip()
        snippet = snippet if len(snippet) <= 200 else snippet[:197] + "…"
        mid = memory.get("id")
        bullets.append(f"- **{title}** (id={mid}, importance={importance}/10) — {snippet}")
    return bullets


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


def _unwrap_single(response: Dict[str, Any]) -> Any:
    """Pull the inner record out of FastMCP's `{"result": <obj>}` wrapping."""
    if not isinstance(response, dict):
        return response
    if set(response.keys()) == {"result"}:
        return response["result"]
    return response
