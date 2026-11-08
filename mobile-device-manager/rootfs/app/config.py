"""Parse and validate the app options (/data/options.json)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
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


def parse_log_level(raw: dict[str, object]) -> str:
    """The ``log_level`` option, lower-cased ("info" when missing or unknown)."""

    value = str(raw.get("log_level") or "info").strip().lower()
    return value if value in LOG_LEVELS else "info"


def apply_log_level(name: str) -> None:
    """Set the root logger to ``name`` (one of LOG_LEVELS)."""

    logging.addLevelName(TRACE, "TRACE")
    logging.addLevelName(NOTICE, "NOTICE")
    logging.getLogger().setLevel(LOG_LEVELS.get(name, logging.INFO))


class ConfigError(ValueError):
    """Raised when the app configuration is invalid."""


@dataclass(slots=True)
class Config:
    """Parsed app options."""

    scan_minutes: int = 30
    stale_days: int = 30
    notify_on_attention: bool = True
    ignored_devices: list[str] = field(default_factory=list)
    log_level: str = "info"


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

    ignored = raw.get("ignored_devices")
    notify = raw.get("notify_on_attention", True)
    return Config(
        scan_minutes=_int(raw, "scan_minutes", 30, 5, 10080),
        stale_days=_int(raw, "stale_days", 30, 1, 365),
        notify_on_attention=notify if isinstance(notify, bool) else True,
        ignored_devices=sorted({str(v).strip() for v in ignored if str(v).strip()})
        if isinstance(ignored, list)
        else [],
        log_level=parse_log_level(raw),
    )
