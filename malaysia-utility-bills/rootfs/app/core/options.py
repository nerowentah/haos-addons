"""Option helpers shared by the app core and every provider (log level, errors, the options file)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

CONFIG_PATH = Path("/data/options.json")

TRACE = 5
NOTICE = 25
LOG_LEVELS = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": NOTICE,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


class ConfigError(ValueError):
    """Raised when the app configuration is invalid."""


def parse_log_level(raw: dict[str, object]) -> str:
    """The ``log_level`` option, lower-cased ("info" when missing or unknown)."""

    value = str(raw.get("log_level") or "info").strip().lower()
    return value if value in LOG_LEVELS else "info"


def apply_log_level(name: str) -> None:
    """Set the root logger to ``name`` (one of LOG_LEVELS)."""

    logging.addLevelName(TRACE, "TRACE")
    logging.addLevelName(NOTICE, "NOTICE")
    logging.getLogger().setLevel(LOG_LEVELS.get(name, logging.INFO))


def slugify(value: str) -> str:
    """Lower-case ``value`` and collapse anything but [a-z0-9] into underscores."""

    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def load_options(path: Path = CONFIG_PATH) -> dict[str, object]:
    """Read and parse the supervisor options file."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"Configuration file not found at {path}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"Invalid JSON in {path}: {err}") from err
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration root in {path} must be a mapping")
    return raw


def option_bool(entry: dict[str, object], key: str, default: bool = True) -> bool:
    """A boolean option (``default`` when missing or not a bool)."""

    value = entry.get(key)
    return value if isinstance(value, bool) else default
