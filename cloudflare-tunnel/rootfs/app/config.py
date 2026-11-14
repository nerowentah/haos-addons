"""Parse and validate the app options (/data/options.json)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, NewType

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

# Flags the user may pass to cloudflared (``run_parameters``). Everything else
# is rejected, so an option edit can never make the connector misbehave.
ALLOWED_RUN_PARAMETERS = (
    "--edge-bind-address",
    "--edge-ip-version",
    "--grace-period",
    "--ha-connections",
    "--protocol",
    "--region",
    "--retries",
    "--tag",
)
_RUN_PARAMETER_RE = re.compile(r"^(--[a-z-]+)=.*$")
# A public DNS name such as home.example.com: no scheme, port or path.
_HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")

type OriginTls = Literal["auto", "verify", "skip"]
ORIGIN_TLS_MODES: tuple[OriginTls, ...] = ("auto", "verify", "skip")  # see origin_tls.py

type Mode = Literal["token", "login"]
MODE_TOKEN: Final = "token"  # noqa: S105 - a mode name, not a secret
MODE_LOGIN: Final = "login"

# A public DNS name that passed parse_hostname ("" when none is set).
Hostname = NewType("Hostname", str)

# cloudflared's own log levels; ours are close but not identical.
CLOUDFLARED_LOGLEVELS: dict[LogLevel, str] = {
    "trace": "debug",
    "debug": "debug",
    "info": "info",
    "notice": "info",
    "warning": "warn",
    "error": "error",
    "fatal": "fatal",
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


def _no_secret() -> Secret:
    return Secret("")


@dataclass(frozen=True, slots=True)
class Config:
    """Parsed app options (never changed in place: new options make a new Config)."""

    tunnel_token: Secret = field(default_factory=_no_secret)  # revealed only in cloudflared's environment
    external_hostname: Hostname = Hostname("")
    notify_on_failure: bool = True
    run_parameters: tuple[str, ...] = ()
    log_level: LogLevel = "info"
    origin_tls: OriginTls = "auto"

    @property
    def mode(self) -> Mode:
        """``token`` (a tunnel made in Zero Trust) or ``login`` (the app signs in and makes the tunnel)."""

        return MODE_TOKEN if self.tunnel_token else MODE_LOGIN

    @property
    def cloudflared_loglevel(self) -> str:
        """cloudflared's ``--loglevel`` for our ``log_level``."""

        return CLOUDFLARED_LOGLEVELS[self.log_level]

    def daemon_spec(self) -> tuple[object, ...]:
        """The part of the connector command that the options control.

        Two specs being equal means a restart is not needed; a different token
        or parameters is applied by restarting the connector. A new
        ``external_hostname`` in login mode is applied by the app itself (DNS
        route, then a connector restart), so it is not part of the spec.
        """

        return (self.mode, self.tunnel_token, self.cloudflared_loglevel, self.run_parameters)


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


def _run_parameters(raw: dict[str, object]) -> tuple[str, ...]:
    rows = raw.get("run_parameters")
    if rows is None:
        return ()
    if not isinstance(rows, list):
        raise ConfigError("run_parameters must be a list")
    kept: list[str] = []
    for row in rows:
        text = str(row or "").strip()
        if not text:
            continue
        match = _RUN_PARAMETER_RE.match(text)
        flag, _, _value = text.partition("=")
        if match is None or flag not in ALLOWED_RUN_PARAMETERS:
            allowed = ", ".join(ALLOWED_RUN_PARAMETERS)
            raise ConfigError(f"run_parameters entry '{text}' is not allowed (allowed: {allowed})")
        kept.append(text)
    return tuple(kept)


def parse_hostname(value: object) -> Hostname:
    """A public hostname, lower-cased ("" when empty); raises ConfigError when it is not one."""

    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        return Hostname("")
    if not _HOSTNAME_RE.match(text):
        raise ConfigError(
            f"external_hostname '{text}' is not a hostname. Use just the name, e.g. home.example.com "
            "(no https://, port or path)."
        )
    return Hostname(text)


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config.

    An empty ``tunnel_token`` is valid: the app then runs in login mode and
    the panel walks through signing in to Cloudflare.
    """

    token = str(raw.get("tunnel_token") or "").strip()
    notify = raw.get("notify_on_failure", True)
    return Config(
        tunnel_token=Secret(token),
        external_hostname=parse_hostname(raw.get("external_hostname")),
        notify_on_failure=notify if isinstance(notify, bool) else True,
        run_parameters=_run_parameters(raw),
        log_level=parse_log_level(raw),
        origin_tls=_origin_tls(raw),
    )


def _origin_tls(raw: dict[str, object]) -> OriginTls:
    value = str(raw.get("origin_tls") or "auto").strip().lower()
    for mode in ORIGIN_TLS_MODES:
        if mode == value:
            return mode
    raise ConfigError(f"origin_tls must be one of {', '.join(ORIGIN_TLS_MODES)}")
