"""Configuration loading for the Forgetful memory provider plugin.

Resolution order (highest precedence first):
  1. Environment variables (FORGETFUL_*)
  2. ``$HERMES_HOME/forgetful.json``
  3. Hard-coded defaults

Secrets (CONTEXT7_API_KEY, POSTGRES_PASSWORD) are env-only — never
persisted to the JSON file. ``save_config()`` writes only non-secret
fields back to the JSON.

The JSON schema is intentionally flat — this plugin doesn't need
multi-host or per-platform sub-blocks. Single user, single backend.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "forgetful.json"

_VALID_RECALL_MODES = ("hybrid", "context", "tools")
_RECALL_MODE_ALIASES = {"auto": "hybrid"}

_VALID_BACKENDS = ("sqlite", "postgres")


def _normalize_recall_mode(value: Any) -> str:
    if not isinstance(value, str):
        return "hybrid"
    norm = _RECALL_MODE_ALIASES.get(value.strip().lower(), value.strip().lower())
    return norm if norm in _VALID_RECALL_MODES else "hybrid"


def _normalize_backend(value: Any) -> str:
    if not isinstance(value, str):
        return "sqlite"
    norm = value.strip().lower()
    return norm if norm in _VALID_BACKENDS else "sqlite"


def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ForgetfulConfig:
    """Resolved configuration for one ForgetfulMemoryProvider instance."""

    # Recall behaviour
    recall_mode: str = "hybrid"
    context_tokens: int = 4000

    # Subprocess
    forgetful_command: str = "uvx"
    forgetful_args: list[str] = field(default_factory=lambda: ["forgetful-ai"])
    startup_timeout: float = 60.0
    call_timeout: float = 30.0

    # Backend selection (controls env vars passed to the subprocess)
    backend: str = "sqlite"
    postgres_host: Optional[str] = None
    postgres_port: Optional[int] = None
    postgres_db: Optional[str] = None
    postgres_user: Optional[str] = None
    sqlite_path: Optional[str] = None

    # Companion integrations
    context7_enabled: bool = False  # set when CONTEXT7_API_KEY is present in env

    # Secrets (env-only, never persisted)
    context7_api_key: Optional[str] = field(default=None, repr=False)
    postgres_password: Optional[str] = field(default=None, repr=False)

    # ---- factory ----------------------------------------------------------

    @classmethod
    def load(cls, hermes_home: Path | str) -> "ForgetfulConfig":
        """Load config: defaults ⇽ JSON file ⇽ environment variables."""
        cfg = cls()
        cfg._apply_file(Path(hermes_home))
        cfg._apply_env()
        cfg._normalize()
        return cfg

    def _apply_file(self, hermes_home: Path) -> None:
        path = hermes_home / CONFIG_FILENAME
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("forgetful: could not parse %s: %s", path, exc)
            return
        if not isinstance(data, dict):
            logger.warning("forgetful: %s is not a JSON object", path)
            return
        valid = {f.name for f in fields(self)}
        for key, value in data.items():
            if key in valid:
                setattr(self, key, value)

    def _apply_env(self) -> None:
        env = os.environ
        if v := env.get("FORGETFUL_RECALL_MODE"):
            self.recall_mode = v
        if v := env.get("FORGETFUL_CONTEXT_TOKENS"):
            self.context_tokens = _coerce_int(v, self.context_tokens) or self.context_tokens
        if v := env.get("FORGETFUL_COMMAND"):
            self.forgetful_command = v
        if v := env.get("FORGETFUL_ARGS"):
            # space-separated
            self.forgetful_args = v.split()
        if v := env.get("FORGETFUL_STARTUP_TIMEOUT"):
            self.startup_timeout = _coerce_float(v, self.startup_timeout)
        if v := env.get("FORGETFUL_CALL_TIMEOUT"):
            self.call_timeout = _coerce_float(v, self.call_timeout)
        if v := env.get("FORGETFUL_BACKEND"):
            self.backend = v
        if v := env.get("FORGETFUL_POSTGRES_HOST"):
            self.postgres_host = v
        if v := env.get("FORGETFUL_POSTGRES_PORT"):
            self.postgres_port = _coerce_int(v, self.postgres_port)
        if v := env.get("FORGETFUL_POSTGRES_DB"):
            self.postgres_db = v
        if v := env.get("FORGETFUL_POSTGRES_USER"):
            self.postgres_user = v
        if v := env.get("FORGETFUL_POSTGRES_PASSWORD"):
            self.postgres_password = v
        if v := env.get("FORGETFUL_SQLITE_PATH"):
            self.sqlite_path = v
        # Context7 is opt-in: presence of the key enables it
        if v := env.get("CONTEXT7_API_KEY"):
            self.context7_api_key = v
            self.context7_enabled = True

    def _normalize(self) -> None:
        self.recall_mode = _normalize_recall_mode(self.recall_mode)
        self.backend = _normalize_backend(self.backend)
        if self.context_tokens < 0:
            self.context_tokens = 0
        if not isinstance(self.forgetful_args, list):
            self.forgetful_args = list(self.forgetful_args or ["forgetful-ai"])

    # ---- subprocess env --------------------------------------------------

    def subprocess_env(self) -> Optional[dict[str, str]]:
        """Build env vars to pass to the forgetful-ai subprocess.

        Returns ``None`` when no overrides are needed (subprocess inherits
        the parent env). Forgetful's settings module reads ``DATABASE``,
        ``POSTGRES_*``, and ``SQLITE_PATH`` directly.
        """
        env: dict[str, str] = {}
        if self.backend == "postgres":
            env["DATABASE"] = "Postgres"
            if self.postgres_host:
                env["POSTGRES_HOST"] = self.postgres_host
            if self.postgres_port is not None:
                env["PGPORT"] = str(self.postgres_port)
            if self.postgres_db:
                env["POSTGRES_DB"] = self.postgres_db
            if self.postgres_user:
                env["POSTGRES_USER"] = self.postgres_user
            if self.postgres_password:
                env["POSTGRES_PASSWORD"] = self.postgres_password
        elif self.backend == "sqlite":
            env["DATABASE"] = "SQLite"
            if self.sqlite_path:
                env["SQLITE_PATH"] = self.sqlite_path

        if not env:
            return None
        merged = dict(os.environ)
        merged.update(env)
        return merged

    # ---- persistence -----------------------------------------------------

    def to_json_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict, omitting secrets."""
        data = asdict(self)
        # Strip secrets and derived flags
        data.pop("context7_api_key", None)
        data.pop("postgres_password", None)
        data.pop("context7_enabled", None)
        return data


def save_config_file(values: dict[str, Any], hermes_home: Path | str) -> Path:
    """Write the given values to ``$HERMES_HOME/forgetful.json``.

    Existing fields are preserved (shallow merge). Returns the path written.
    """
    hermes_home = Path(hermes_home)
    hermes_home.mkdir(parents=True, exist_ok=True)
    path = hermes_home / CONFIG_FILENAME

    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text())
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, json.JSONDecodeError):
            existing = {}

    # Drop secret fields and unknown keys
    valid = {f.name for f in fields(ForgetfulConfig)}
    secrets = {"context7_api_key", "postgres_password"}
    cleaned = {
        k: v for k, v in values.items() if k in valid and k not in secrets
    }
    existing.update(cleaned)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    logger.info("forgetful: config written to %s", path)
    return path
