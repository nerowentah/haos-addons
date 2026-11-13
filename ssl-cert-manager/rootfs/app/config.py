"""Parse and validate the app options (/data/options.json)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
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
RENEW_DAYS_MIN, RENEW_DAYS_MAX, RENEW_DAYS_DEFAULT = 7, 60, 30
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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

    cloudflare_api_token: str = ""
    acme_email: str = ""
    acme_staging: bool = False
    local_proxy: bool = False
    write_ssl_root: bool = True
    renew_days_before: int = RENEW_DAYS_DEFAULT
    notify_on_failure: bool = True
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


def _bool(raw: dict[str, object], name: str, default: bool) -> bool:
    value = raw.get(name, default)
    return value if isinstance(value, bool) else default


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config (everything is optional)."""

    email = str(raw.get("acme_email") or "").strip()
    if email and not _EMAIL_RE.match(email):
        raise ConfigError(f"acme_email '{email}' is not an e-mail address")
    days = raw.get("renew_days_before", RENEW_DAYS_DEFAULT)
    if isinstance(days, bool) or not isinstance(days, int) or not RENEW_DAYS_MIN <= days <= RENEW_DAYS_MAX:
        raise ConfigError(f"renew_days_before must be a whole number from {RENEW_DAYS_MIN} to {RENEW_DAYS_MAX}")
    token = str(raw.get("cloudflare_api_token") or "").strip()
    if token and (len(token) < 20 or any(c.isspace() for c in token)):  # noqa: PLR2004 - Cloudflare tokens are 40 characters
        raise ConfigError("cloudflare_api_token does not look like a Cloudflare API token (copy it again)")
    return Config(
        cloudflare_api_token=token,
        acme_email=email,
        acme_staging=_bool(raw, "acme_staging", False),
        local_proxy=_bool(raw, "local_proxy", False),
        write_ssl_root=_bool(raw, "write_ssl_root", True),
        renew_days_before=days,
        notify_on_failure=_bool(raw, "notify_on_failure", True),
        log_level=parse_log_level(raw),
    )
