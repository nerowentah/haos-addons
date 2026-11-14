"""Parse and validate the app options (/data/options.json)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CONFIG_PATH = Path("/data/options.json")

TRACE = 5
NOTICE = 25
type LogLevel = Literal["trace", "debug", "info", "notice", "warning", "error", "fatal"]
LOG_LEVELS: dict[LogLevel, int] = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": NOTICE,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def parse_log_level(raw: dict[str, object]) -> LogLevel:
    """The ``log_level`` option, lower-cased ("info" when missing or unknown)."""

    value = str(raw.get("log_level") or "info").strip().lower()
    for level in LOG_LEVELS:
        if level == value:
            return level
    return "info"


def apply_log_level(name: LogLevel) -> None:
    """Set the root logger to ``name``."""

    logging.addLevelName(TRACE, "TRACE")
    logging.addLevelName(NOTICE, "NOTICE")
    logging.getLogger().setLevel(LOG_LEVELS[name])


class ConfigError(ValueError):
    """Raised when the app configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Parsed app options (never changed in place: new options make a new Config)."""

    unused_days: int = 90
    notify_on_new_keys: bool = True
    vague_words: tuple[str, ...] = ()
    log_level: LogLevel = "info"


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


def _int(raw: dict[str, object], key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, low), high)


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config."""

    words = raw.get("vague_words")
    notify = raw.get("notify_on_new_keys", True)
    return Config(
        unused_days=_int(raw, "unused_days", 90, 7, 3650),
        notify_on_new_keys=notify if isinstance(notify, bool) else True,
        vague_words=tuple(sorted({str(v).strip().lower() for v in words if str(v).strip()}))
        if isinstance(words, list)
        else (),
        log_level=parse_log_level(raw),
    )
