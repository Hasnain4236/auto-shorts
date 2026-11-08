"""Config loading: config.yaml + environment variables (.env for local runs)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

REQUIRED_KEYS = [
    "channel.niche",
    "channel.language",
    "trends",
    "llm.providers",
    "video.resolution",
    "upload",
]


class ConfigError(Exception):
    pass


@dataclass
class Config:
    raw: dict[str, Any]
    root: Path
    runs_dir: Path
    ledger_path: Path
    todos: list[str] = field(default_factory=list)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        value = self.get(dotted)
        if value is None:
            raise ConfigError(f"Missing config key: {dotted}")
        return value


def env(name: str, required: bool = True) -> str | None:
    """Read a secret from the environment. Secrets never live in config.yaml."""
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise ConfigError(f"Environment variable {name} is not set (see .env.example)")
        return None
    return value


def _find_todos(node: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += _find_todos(v, f"{prefix}{k}.")
    elif isinstance(node, str) and node.strip().upper().startswith("TODO"):
        found.append(prefix.rstrip("."))
    return found


def load_config(path: Path | str | None = None) -> Config:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:  # optional for CI where secrets come from the env
        pass

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    probe = Config(raw=raw, root=PROJECT_ROOT, runs_dir=Path(), ledger_path=Path())
    missing = [k for k in REQUIRED_KEYS if probe.get(k) is None]
    if missing:
        raise ConfigError(f"config.yaml is missing required keys: {', '.join(missing)}")

    runs_env = os.environ.get("RUNS_DIR", "").strip()
    runs_dir = Path(runs_env) if runs_env else PROJECT_ROOT / probe.get("paths.runs_dir", "runs")
    ledger_path = PROJECT_ROOT / probe.get("paths.ledger", "data/uploaded_ledger.json")

    todos = _find_todos(raw)
    for key in todos:
        log.warning("config.yaml: %s is still TODO", key)

    return Config(raw=raw, root=PROJECT_ROOT, runs_dir=runs_dir, ledger_path=ledger_path, todos=todos)
