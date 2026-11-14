"""Parse and validate the app options (/data/options.json)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from redact import Secret

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
RENEW_DAYS_MIN, RENEW_DAYS_MAX, RENEW_DAYS_DEFAULT = 7, 60, 30
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HSTS_RE = re.compile(r"^max-age=\d+(;\s*includeSubDomains)?(;\s*preload)?$", re.IGNORECASE)
type RealIp = Literal["off", "apps", "cloudflare"]
REAL_IP_MODES: tuple[RealIp, ...] = ("off", "apps", "cloudflare")  # see proxy.py


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


def _no_secret() -> Secret:
    return Secret("")


@dataclass(frozen=True, slots=True)
class Config:
    """Parsed app options (never changed in place: new options make a new Config)."""

    cloudflare_api_token: Secret = field(default_factory=_no_secret)  # revealed only to certbot and Cloudflare
    acme_email: str = ""
    acme_staging: bool = False
    local_proxy: bool = False
    write_ssl_root: bool = True
    renew_days_before: int = RENEW_DAYS_DEFAULT
    proxy_hsts: str = ""
    proxy_real_ip: RealIp = "apps"
    notify_on_failure: bool = True
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
    hsts = str(raw.get("proxy_hsts") or "").strip()
    if hsts and not _HSTS_RE.match(hsts):
        raise ConfigError(f"proxy_hsts '{hsts}' is not a Strict-Transport-Security value, e.g. max-age=31536000")
    real_ip_text = str(raw.get("proxy_real_ip") or "apps").strip().lower()
    real_ip = next((mode for mode in REAL_IP_MODES if mode == real_ip_text), None)
    if real_ip is None:
        raise ConfigError(f"proxy_real_ip must be one of {', '.join(REAL_IP_MODES)}")
    return Config(
        cloudflare_api_token=Secret(token),
        acme_email=email,
        acme_staging=_bool(raw, "acme_staging", False),
        local_proxy=_bool(raw, "local_proxy", False),
        write_ssl_root=_bool(raw, "write_ssl_root", True),
        renew_days_before=days,
        proxy_hsts=hsts,
        proxy_real_ip=real_ip,
        notify_on_failure=_bool(raw, "notify_on_failure", True),
        log_level=parse_log_level(raw),
    )
