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
KEEP_BACKUPS_MIN, KEEP_BACKUPS_MAX, KEEP_BACKUPS_DEFAULT = 1, 50, 3
LOW_SPACE_MIN, LOW_SPACE_MAX, LOW_SPACE_DEFAULT = 1, 50, 10


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
    """Parsed app options (never changed in place: new options make a new Config).

    Everything that deletes on its own is off unless the options say otherwise.
    """

    auto_clean: bool = False
    auto_clean_backups: bool = False  # only acts while auto_clean is on as well
    keep_backups: int = KEEP_BACKUPS_DEFAULT
    low_space_percent: int = LOW_SPACE_DEFAULT
    notify_on_low_space: bool = True
    log_level: LogLevel = "info"

    @property
    def backups_auto_deleted(self) -> bool:
        """Whether the daily clean-up may delete backups: both switches must be on."""

        return self.auto_clean and self.auto_clean_backups


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


def _switch(raw: dict[str, object], name: str, default: bool) -> bool:
    """A true/false option. Anything that is not exactly true or false is an error, never a guess."""

    value = raw.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _whole(raw: dict[str, object], name: str, default: int, low: int, high: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{name} must be a whole number from {low} to {high}")
    return value


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config (everything is optional)."""

    return Config(
        auto_clean=_switch(raw, "auto_clean", False),
        auto_clean_backups=_switch(raw, "auto_clean_backups", False),
        keep_backups=_whole(raw, "keep_backups", KEEP_BACKUPS_DEFAULT, KEEP_BACKUPS_MIN, KEEP_BACKUPS_MAX),
        low_space_percent=_whole(raw, "low_space_percent", LOW_SPACE_DEFAULT, LOW_SPACE_MIN, LOW_SPACE_MAX),
        notify_on_low_space=_switch(raw, "notify_on_low_space", True),
        log_level=parse_log_level(raw),
    )
