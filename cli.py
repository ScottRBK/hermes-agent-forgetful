"""``hermes forgetful`` subcommand tree.

Two entry points:
- ``cmd_setup`` is invoked by ``hermes memory setup`` (via the provider's
  ``post_setup`` hook) AND by ``hermes forgetful setup``.
- ``register_cli(subparser)`` wires the runtime commands (status, project,
  search, save, list, gather, explore, encode, reset) into argparse.

Higher-level commands (gather, explore, encode) delegate to the modules
that own them so this file stays a thin dispatcher.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

# Hermes-agent's lightweight CLI scan
# (``plugins/memory/__init__.py:discover_plugin_cli_commands``) imports
# this module standalone via ``spec_from_file_location`` — without first
# registering the parent package in ``sys.modules``. That breaks the
# relative imports below ("attempted relative import with no known parent
# package"). When ``__name__`` is dotted (i.e. we were loaded as a
# submodule), synthesise a minimal parent stub with a correct ``__path__``
# so Python's import machinery can resolve ``from .client import ...``.
# When the plugin is loaded as a full package by ``_load_provider_from_dir``
# (which DOES set up the parent), this branch is a no-op.
if "." in __name__:
    _parent_name = __name__.rsplit(".", 1)[0]
    if _parent_name not in sys.modules:
        _stub = types.ModuleType(_parent_name)
        _stub.__path__ = [str(Path(__file__).resolve().parent)]
        sys.modules[_parent_name] = _stub

from ._install_paths import (
    install_name_from_path,
    register_skills_dir_in_config,
)
from .client import ForgetfulClient, ForgetfulClientError
from .config import ForgetfulConfig, save_config_file

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# I/O helpers (kept tiny — no curses, no rich; works headless)
# ---------------------------------------------------------------------------

def _print(msg: str = "") -> None:
    print(msg, flush=True)


def _prompt(label: str, *, default: Optional[str] = None) -> str:
    """Prompt with default; returns the default verbatim under non-TTY."""
    if not sys.stdin.isatty():
        return default or ""
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"  {label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        _print()
        return default or ""
    return raw or (default or "")


def _prompt_choice(label: str, choices: List[str], default: str) -> str:
    pretty = "/".join(c if c != default else c.upper() for c in choices)
    while True:
        raw = _prompt(f"{label} ({pretty})", default=default).lower()
        if raw in choices:
            return raw
        _print(f"  Please choose one of: {', '.join(choices)}")


def _extract_memory_list(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull a list of memory dicts out of a forgetful tool response.

    FastMCP wraps non-dict returns under a single ``result`` key; some
    tools also return ``memories`` or ``results``. Normalises across all
    three shapes.
    """
    for key in ("memories", "results", "result"):
        value = response.get(key)
        if isinstance(value, list):
            return [m for m in value if isinstance(m, dict)]
    return []


def _detect_git_remote(cwd: Path) -> Optional[str]:
    """Return the git remote 'origin' URL for *cwd*, or None."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cwd), capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    url = (result.stdout or "").strip()
    return url or None


def _project_name_from_remote(url: str) -> Optional[str]:
    """Extract repo name from common git URL forms."""
    if not url:
        return None
    name = url.rstrip("/")
    if name.endswith(".git"):
        name = name[:-4]
    name = name.split("/")[-1]
    return name or None


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def cmd_setup(
    *,
    hermes_home: str,
    hermes_config: Optional[Dict[str, Any]] = None,
) -> None:
    """Interactive setup wizard for the Forgetful provider.

    Walks the user through:
      1. uvx availability check
      2. recall mode selection
      3. project binding (detect git remote → match existing or create)
      4. forgetful.json + hermes config.yaml persistence

    Falls back to non-interactive defaults under ``not sys.stdin.isatty()``
    (CI / scripted setup).
    """
    import shutil

    _print("\n  Forgetful Memory Provider Setup")
    _print("  ───────────────────────────────")

    if shutil.which("uvx") is None:
        _print("\n  ✗ uvx is not on PATH. Install astral-sh/uv first:")
        _print("    curl -LsSf https://astral.sh/uv/install.sh | sh")
        _print("\n  Setup aborted.\n")
        return

    cfg_existing = ForgetfulConfig.load(hermes_home)

    # Recall mode
    recall_mode = _prompt_choice(
        "Recall mode",
        choices=["hybrid", "context", "tools"],
        default=cfg_existing.recall_mode or "hybrid",
    )

    # Project binding
    cwd = Path.cwd()
    remote = _detect_git_remote(cwd)
    default_project_name = (
        cfg_existing.project_name
        or _project_name_from_remote(remote or "")
        or cwd.name
    )
    project_name = _prompt(
        "Project name (blank to skip project binding)",
        default=default_project_name,
    ).strip()

    project_id: Optional[int] = None
    if project_name:
        _print(f"\n  Connecting to forgetful-ai (uvx) — first run installs the package…")
        client = ForgetfulClient(
            command=cfg_existing.forgetful_command,
            args=list(cfg_existing.forgetful_args),
            env=cfg_existing.subprocess_env(),
            startup_timeout=max(cfg_existing.startup_timeout, 90.0),
        )
        try:
            client.start()
            project_id = _ensure_project(client, project_name, remote)
            _print(f"  ✓ Bound to project: {project_name} (id={project_id})")
        except ForgetfulClientError as exc:
            _print(f"  ✗ Could not connect to forgetful-ai: {exc}")
            _print("    Continuing without a project binding — you can re-run "
                   "`hermes forgetful project switch <name>` later.")
        finally:
            client.close()
    else:
        _print("\n  Skipping project binding (writes will be unscoped).")

    # Persist forgetful.json
    save_values: Dict[str, Any] = {
        "recall_mode": recall_mode,
    }
    if project_id is not None:
        save_values["project_id"] = project_id
        save_values["project_name"] = project_name

    cfg_path = save_config_file(save_values, hermes_home)
    _print(f"\n  ✓ Wrote forgetful config to {cfg_path}")

    # Hermes-agent's memory-provider discovery keys on the *directory name*
    # under ``<hermes_home>/plugins/`` (see plugins/memory/__init__.py
    # _iter_provider_dirs). When the user clones this plugin under the
    # GitHub repo name (e.g. ``hermes-agent-forgetful``), we must write
    # that actual dir name as ``memory.provider`` — writing the manifest
    # ``name: forgetful`` instead would make discovery silently fail.
    install_name = install_name_from_path(Path(__file__).parent)

    # Activate provider + register plugin skills dir in hermes config.yaml
    if hermes_config is not None:
        try:
            from hermes_cli.config import save_config
            mem = hermes_config.setdefault("memory", {})
            mem["provider"] = install_name

            # Register the plugin's skills/ as an external skills directory so
            # the agent can discover encode-repo (and any future skills shipped
            # with this plugin) automatically.
            register_skills_dir_in_config(hermes_config, hermes_home, install_name)

            save_config(hermes_config)
            _print(f"  ✓ memory.provider = '{install_name}' saved to hermes config.yaml")
            _print("  ✓ plugin skills directory registered in skills.external_dirs")
        except Exception as exc:  # noqa: BLE001
            _print(f"  ⚠  Could not update hermes config.yaml: {exc}")
            _print(f"    Set memory.provider: {install_name} manually.")

    _print("\n  Setup complete. Try:")
    _print(f"    hermes {install_name} status")
    _print(f"    hermes {install_name} search 'something you've discussed before'")
    _print("    hermes")
    _print()


def _ensure_project(
    client: ForgetfulClient,
    name: str,
    remote: Optional[str],
) -> int:
    """Return the id of project *name*, creating it if it doesn't exist."""
    listing = client.execute("execute_forgetful_tool", {
        "tool_name": "list_projects",
        "arguments": {},
    })
    projects = listing.get("projects") or []
    for p in projects:
        if isinstance(p, dict) and p.get("name") == name:
            return int(p["id"])

    description = (
        f"Auto-created by hermes forgetful setup for {remote}"
        if remote else
        "Auto-created by hermes forgetful setup"
    )
    created = client.execute("execute_forgetful_tool", {
        "tool_name": "create_project",
        "arguments": {
            "name": name,
            "description": description,
            "project_type": "development",
        },
    })
    proj = created.get("project") or created
    pid = proj.get("id")
    if pid is None:
        raise ForgetfulClientError(
            f"create_project response missing 'id': {created}"
        )
    return int(pid)


# ---------------------------------------------------------------------------
# Runtime CLI commands
# ---------------------------------------------------------------------------

def _open_client(cfg: ForgetfulConfig) -> ForgetfulClient:
    """Open a short-lived ForgetfulClient for a CLI command."""
    client = ForgetfulClient(
        command=cfg.forgetful_command,
        args=list(cfg.forgetful_args),
        env=cfg.subprocess_env(),
        startup_timeout=cfg.startup_timeout,
        call_timeout=cfg.call_timeout,
    )
    client.start()
    return client


def _resolve_hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")


def cmd_status(args) -> None:
    """Print Forgetful provider health and config summary."""
    home = _resolve_hermes_home()
    cfg = ForgetfulConfig.load(home)

    _print("\n  Forgetful Status")
    _print("  ────────────────")
    _print(f"  HERMES_HOME:  {home}")
    _print(f"  Recall mode:  {cfg.recall_mode}")
    _print(f"  Project:      {cfg.project_name or '(unset)'} "
           f"(id={cfg.project_id if cfg.project_id is not None else '—'})")
    _print(f"  Backend:      {cfg.backend}")
    _print(f"  Command:      {cfg.forgetful_command} {' '.join(cfg.forgetful_args)}")
    _print(f"  Context7:     {'enabled (CONTEXT7_API_KEY set)' if cfg.context7_enabled else 'disabled'}")

    _print("\n  Probing subprocess…")
    try:
        client = _open_client(cfg)
    except ForgetfulClientError as exc:
        _print(f"  ✗ Could not start forgetful-ai: {exc}\n")
        return
    try:
        meta = client.execute("discover_forgetful_tools", {})
        _print(f"  ✓ Connected — {meta.get('total_count', '?')} backend tools available")
        if cfg.project_id is not None:
            recent = client.execute("execute_forgetful_tool", {
                "tool_name": "get_recent_memories",
                "arguments": {"limit": 5, "project_ids": [cfg.project_id]},
            })
            mems = _extract_memory_list(recent)
            _print(f"  ✓ {len(mems)} recent memories in project")
    except ForgetfulClientError as exc:
        _print(f"  ⚠ Probe failed: {exc}")
    finally:
        client.close()
    _print()


def cmd_project(args) -> None:
    """Show / switch / list forgetful projects."""
    home = _resolve_hermes_home()
    cfg = ForgetfulConfig.load(home)
    sub = (args.project_command or "show").lower()

    if sub == "show":
        if cfg.project_id is None:
            _print("\n  No project bound. Run `hermes forgetful project switch <name>`.\n")
            return
        _print(f"\n  Active project: {cfg.project_name} (id={cfg.project_id})\n")
        return

    if sub == "list":
        client = _open_client(cfg)
        try:
            res = client.execute("execute_forgetful_tool", {
                "tool_name": "list_projects",
                "arguments": {},
            })
            projects = res.get("projects") or []
        finally:
            client.close()
        _print()
        if not projects:
            _print("  (no projects)")
        for p in projects:
            marker = " *" if isinstance(p, dict) and p.get("id") == cfg.project_id else "  "
            _print(f"  {marker}{p.get('id'):>4}  {p.get('name')}  — {p.get('description', '')[:60]}")
        _print()
        return

    if sub == "switch":
        name = args.project_name
        if not name:
            _print("\n  Usage: hermes forgetful project switch <name>\n")
            return
        client = _open_client(cfg)
        try:
            pid = _ensure_project(client, name, _detect_git_remote(Path.cwd()))
        finally:
            client.close()
        save_config_file({"project_id": pid, "project_name": name}, home)
        _print(f"\n  ✓ Switched to project: {name} (id={pid})\n")
        return

    _print(f"\n  Unknown project subcommand: {sub}\n")


def cmd_search(args) -> None:
    """Quick semantic search from the shell."""
    home = _resolve_hermes_home()
    cfg = ForgetfulConfig.load(home)
    query = " ".join(args.query) if isinstance(args.query, list) else args.query
    if not query:
        _print("\n  Usage: hermes forgetful search <query>\n")
        return

    payload: Dict[str, Any] = {
        "query": query,
        "query_context": "CLI semantic search",
        "k": max(1, min(args.k or 5, 20)),
        "include_links": True,
    }
    if args.scope == "current" and cfg.project_id is not None:
        payload["project_ids"] = [cfg.project_id]
        payload["strict_project_filter"] = True

    client = _open_client(cfg)
    try:
        res = client.execute("execute_forgetful_tool", {
            "tool_name": "query_memory",
            "arguments": payload,
        })
    finally:
        client.close()

    primaries = res.get("primary_memories") or []
    _print()
    if not primaries:
        _print("  (no results)\n")
        return
    for m in primaries:
        title = (m.get("title") or "(untitled)").strip()
        importance = m.get("importance")
        mid = m.get("id")
        proj_ids = m.get("project_ids") or []
        _print(f"  [{importance}/10] {title}  (id={mid}, projects={proj_ids})")
        body = (m.get("content") or "").strip()
        if body:
            preview = body if len(body) <= 240 else body[:237] + "…"
            for line in preview.splitlines():
                _print(f"      {line}")
        _print()


def cmd_save(args) -> None:
    """Create a new memory from the shell (interactive or argv)."""
    home = _resolve_hermes_home()
    cfg = ForgetfulConfig.load(home)

    title = args.title or _prompt("Title (5-200 chars)")
    content = args.content
    if not content and sys.stdin.isatty():
        _print("  Content (end with a blank line):")
        lines: List[str] = []
        while True:
            try:
                line = input("  ")
            except EOFError:
                break
            if not line:
                break
            lines.append(line)
        content = "\n".join(lines)
    elif not content and not sys.stdin.isatty():
        content = sys.stdin.read()
    context = args.context or _prompt("Context (WHY this matters)")
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    importance = args.importance or 7

    if not title or not content or not context:
        _print("\n  ✗ title, content, and context are all required.\n")
        return

    payload: Dict[str, Any] = {
        "title": title[:200],
        "content": content[:2000],
        "context": context[:500],
        "keywords": [],
        "tags": tags,
        "importance": max(1, min(importance, 10)),
        "encoding_agent": "hermes-cli/forgetful-plugin",
    }
    if cfg.project_id is not None:
        payload["project_ids"] = [cfg.project_id]

    client = _open_client(cfg)
    try:
        res = client.execute("execute_forgetful_tool", {
            "tool_name": "create_memory",
            "arguments": payload,
        })
    finally:
        client.close()

    mid = res.get("id") or (res.get("memory") or {}).get("id")
    _print(f"\n  ✓ Created memory id={mid}\n")


def cmd_list(args) -> None:
    """List recent memories (newest first)."""
    home = _resolve_hermes_home()
    cfg = ForgetfulConfig.load(home)
    payload: Dict[str, Any] = {"limit": max(1, min(args.limit or 10, 50))}
    if args.project == "current" and cfg.project_id is not None:
        payload["project_ids"] = [cfg.project_id]

    client = _open_client(cfg)
    try:
        res = client.execute("execute_forgetful_tool", {
            "tool_name": "get_recent_memories",
            "arguments": payload,
        })
    finally:
        client.close()
    mems = _extract_memory_list(res)
    _print()
    if not mems:
        _print("  (no recent memories)\n")
        return
    for m in mems:
        ts = m.get("created_at") or ""
        title = (m.get("title") or "(untitled)").strip()
        _print(f"  {ts[:19]}  [{m.get('importance')}/10]  id={m.get('id')}  {title}")
    _print()


def cmd_gather(args) -> None:
    """Run the multi-source context-gather workflow (Forgetful + optional Context7)."""
    from .context_gather import run_gather
    home = _resolve_hermes_home()
    cfg = ForgetfulConfig.load(home)
    task = " ".join(args.task) if isinstance(args.task, list) else args.task
    if not task:
        _print("\n  Usage: hermes forgetful gather <task description>\n")
        return
    client = _open_client(cfg)
    try:
        report = run_gather(
            client=client, config=cfg, task=task,
            frameworks=args.frameworks or [],
            include_web=args.include_web,
        )
    finally:
        client.close()
    _print("\n" + report + "\n")


def cmd_explore(args) -> None:
    """Run the 5-phase deep graph traversal report."""
    from .explore import run_explore
    home = _resolve_hermes_home()
    cfg = ForgetfulConfig.load(home)
    topic = " ".join(args.topic) if isinstance(args.topic, list) else args.topic
    if not topic:
        _print("\n  Usage: hermes forgetful explore <topic>\n")
        return
    client = _open_client(cfg)
    try:
        report = run_explore(
            client=client, config=cfg, topic=topic, depth=args.depth or "medium",
        )
    finally:
        client.close()
    _print("\n" + report + "\n")


def cmd_encode(args) -> None:
    """Spawn a hermes batch session running the encoder pipeline against a repo."""
    from .encoder.runner import run_encode
    target = Path(args.path).resolve()
    if not target.is_dir():
        _print(f"\n  ✗ Not a directory: {target}\n")
        return
    rc = run_encode(
        target=target,
        profile=args.profile,
        dry_run=args.dry_run,
    )
    sys.exit(rc)


def cmd_reset(args) -> None:
    """Tear down forgetful.json so setup can be re-run cleanly."""
    home = Path(_resolve_hermes_home())
    path = home / "forgetful.json"
    if path.is_file():
        path.unlink()
        _print(f"\n  ✓ Removed {path}\n")
    else:
        _print(f"\n  (nothing to reset — {path} does not exist)\n")


# ---------------------------------------------------------------------------
# argparse registration (entry point used by hermes plugin loader)
# ---------------------------------------------------------------------------

def _dispatch(args) -> None:
    sub = getattr(args, "forgetful_command", None)
    handlers = {
        "setup":   lambda a: cmd_setup(hermes_home=_resolve_hermes_home()),
        "status":  cmd_status,
        "project": cmd_project,
        "search":  cmd_search,
        "save":    cmd_save,
        "list":    cmd_list,
        "gather":  cmd_gather,
        "explore": cmd_explore,
        "encode":  cmd_encode,
        "reset":   cmd_reset,
    }
    handler = handlers.get(sub)
    if handler is None:
        _print("Run `hermes forgetful --help` for usage.")
        return
    try:
        handler(args)
    except ForgetfulClientError as exc:
        _print(f"\n  ✗ {exc}\n")
        sys.exit(2)
    except KeyboardInterrupt:
        _print("\n  Interrupted.")
        sys.exit(130)


def register_cli(subparser) -> None:
    """Build the ``hermes forgetful`` argparse subcommand tree."""
    subs = subparser.add_subparsers(dest="forgetful_command")

    subs.add_parser("setup", help="Run the Forgetful setup wizard")

    subs.add_parser("status", help="Show provider health and config summary")

    project = subs.add_parser("project", help="Show/list/switch project binding")
    project.add_argument(
        "project_command", nargs="?", choices=("show", "list", "switch"), default="show",
    )
    project.add_argument("project_name", nargs="?", default=None)

    search = subs.add_parser("search", help="Semantic search across memories")
    search.add_argument("query", nargs="+")
    search.add_argument("-k", type=int, default=5, help="Number of results (1-20, default 5)")
    search.add_argument(
        "--scope", choices=("all", "current"), default="all",
        help="all (default) searches every project; current restricts to the active project",
    )

    save = subs.add_parser("save", help="Create a memory from the shell")
    save.add_argument("--title")
    save.add_argument("--content")
    save.add_argument("--context")
    save.add_argument("--tags", help="Comma-separated tag list")
    save.add_argument("--importance", type=int, default=7)

    listp = subs.add_parser("list", help="Show recent memories")
    listp.add_argument("--limit", type=int, default=10)
    listp.add_argument("--project", choices=("all", "current"), default="all")

    gather = subs.add_parser(
        "gather", help="Multi-source context gather (Forgetful + optional Context7)",
    )
    gather.add_argument("task", nargs="+", help="Task description")
    gather.add_argument(
        "--frameworks", nargs="*", default=None,
        help="Library/framework hints for Context7 lookup",
    )
    gather.add_argument("--include-web", action="store_true")

    explore = subs.add_parser("explore", help="5-phase graph traversal report")
    explore.add_argument("topic", nargs="+")
    explore.add_argument(
        "--depth", choices=("shallow", "medium", "deep"), default="medium",
    )

    encode = subs.add_parser("encode", help="Multi-phase repo encoder")
    encode.add_argument("path", help="Path to the target repository")
    encode.add_argument(
        "--profile", choices=("small", "medium", "large"), default=None,
    )
    encode.add_argument("--dry-run", action="store_true")

    subs.add_parser("reset", help="Remove forgetful.json (re-run setup afterwards)")

    subparser.set_defaults(func=_dispatch)
