"""Spawn a hermes batch session that runs the encoder pipeline.

The encoder is fundamentally user-initiated, long-running, and benefits
from a clean isolated agent session — so we don't expose it as a
mid-conversation tool. The CLI command ``hermes forgetful encode <path>``
calls ``run_encode``, which:

1. Loads the encoder prompt template (``prompt.md``) and templates in
   the absolute repo path, profile, and active Forgetful project.
2. If ``--dry-run``, prints the rendered prompt and the command that
   would be executed, then exits 0 without spawning a session.
3. Otherwise spawns ``hermes -z <prompt>`` with ``cwd`` set to the
   target repo so file tools resolve against the encoded project.
   Output is streamed unmodified to the parent's stdout/stderr.

Per §13a we DO NOT pass ``--model``, ``--provider``, or any other
flag that would override the user's configured model.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..config import ForgetfulConfig

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompt.md"


def run_encode(
    *,
    target: Path,
    profile: Optional[str] = None,
    dry_run: bool = False,
) -> int:
    """Encode the repository at ``target`` via a hermes batch session.

    Returns the subprocess exit code (0 on success, non-zero on failure).
    """
    target = target.resolve()
    if not target.is_dir():
        print(f"\n  ✗ Not a directory: {target}\n", file=sys.stderr)
        return 1

    profile = (profile or _detect_profile(target)).lower()
    if profile not in ("small", "small_complex", "medium", "large"):
        print(f"\n  ⚠ Unknown profile '{profile}', defaulting to 'medium'", file=sys.stderr)
        profile = "medium"

    cfg = ForgetfulConfig.load(_resolve_hermes_home())
    project_name = cfg.project_name or target.name
    project_id = cfg.project_id

    if project_id is None:
        print(
            "\n  ✗ No active Forgetful project. Run `hermes forgetful project switch <name>`\n"
            "    or `hermes forgetful setup` first so encoded memories have a home.\n",
            file=sys.stderr,
        )
        return 1

    prompt = _render_prompt(
        repo_path=str(target),
        profile=profile,
        project_name=project_name,
        project_id=project_id,
    )

    hermes_bin = shutil.which("hermes")
    if hermes_bin is None:
        print(
            "\n  ✗ `hermes` is not on PATH — cannot spawn encoder batch session.\n",
            file=sys.stderr,
        )
        return 1

    command = [hermes_bin, "-z", prompt]

    print(f"\n  Encoding {target} (profile={profile}, project={project_name}/id={project_id})")
    print(f"  Spawning: {hermes_bin} -z <{len(prompt)} char prompt>  (cwd={target})\n")

    if dry_run:
        print("  --dry-run: prompt rendered below, no session spawned.\n")
        print("─" * 72)
        print(prompt)
        print("─" * 72)
        return 0

    try:
        proc = subprocess.run(  # noqa: S603 — explicit binary path
            command,
            cwd=str(target),
            check=False,
        )
        return proc.returncode
    except KeyboardInterrupt:
        print("\n  Interrupted.\n", file=sys.stderr)
        return 130


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_prompt(
    *, repo_path: str, profile: str, project_name: str, project_id: int,
) -> str:
    """Read prompt.md and substitute templating tokens."""
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template
        .replace("{{REPO_PATH}}", repo_path)
        .replace("{{PROFILE}}", profile)
        .replace("{{PROJECT_NAME}}", project_name)
        .replace("{{PROJECT_ID}}", str(project_id))
    )


_PROFILE_EXCLUDED_DIR_PARTS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "vendor", "third_party", "third-party",
    ".venv", "venv", "env", ".env", ".venv-smoke",
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".tox", "dist", "build", "target", ".next", ".nuxt", ".cache",
    "site-packages",
})

_PROFILE_CODE_SUFFIXES = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb",
    ".java", ".kt", ".scala", ".cs", ".cpp", ".c", ".h", ".hpp",
    ".swift", ".php", ".sql",
})


def _detect_profile(target: Path) -> str:
    """Heuristic profile detection from source-file count.

    Walks the tree but skips common vendor / cache / build directories
    that would otherwise inflate the count by orders of magnitude.
    Bands are loose; the prompt itself tells the agent to stay within
    its band rather than chasing exact numbers.
    """
    n = 0
    try:
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in _PROFILE_EXCLUDED_DIR_PARTS]
            for fn in files:
                if Path(fn).suffix in _PROFILE_CODE_SUFFIXES:
                    n += 1
                    if n >= 5000:  # bail out — definitely large
                        return "large"
    except (OSError, PermissionError):
        return "medium"

    if n < 25:
        return "small"
    if n < 100:
        return "small_complex"
    if n < 500:
        return "medium"
    return "large"


def _resolve_hermes_home() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
