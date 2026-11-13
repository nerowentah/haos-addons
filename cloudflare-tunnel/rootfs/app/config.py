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

ORIGIN_TLS_MODES = ("auto", "verify", "skip")  # see origin_tls.py

MODE_TOKEN = "token"  # noqa: S105 - a mode name, not a secret
MODE_LOGIN = "login"

# cloudflared's own log levels; ours are close but not identical.
CLOUDFLARED_LOGLEVELS = {
    "trace": "debug",
    "debug": "debug",
    "info": "info",
    "notice": "info",
    "warning": "warn",
    "error": "error",
    "fatal": "fatal",
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

    tunnel_token: str = ""
    external_hostname: str = ""
    notify_on_failure: bool = True
    run_parameters: tuple[str, ...] = ()
    log_level: str = "info"
    origin_tls: str = "auto"

    @property
    def mode(self) -> str:
        """``token`` (a tunnel made in Zero Trust) or ``login`` (the app signs in and makes the tunnel)."""

        return MODE_TOKEN if self.tunnel_token else MODE_LOGIN

    @property
    def cloudflared_loglevel(self) -> str:
        """cloudflared's ``--loglevel`` for our ``log_level``."""

        return CLOUDFLARED_LOGLEVELS[self.log_level]

    def daemon_spec(self) -> tuple[str, ...]:
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


def parse_hostname(value: object) -> str:
    """A public hostname, lower-cased ("" when empty); raises ConfigError when it is not one."""

    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        return ""
    if not _HOSTNAME_RE.match(text):
        raise ConfigError(
            f"external_hostname '{text}' is not a hostname. Use just the name, e.g. home.example.com "
            "(no https://, port or path)."
        )
    return text


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config.

    An empty ``tunnel_token`` is valid: the app then runs in login mode and
    the panel walks through signing in to Cloudflare.
    """

    token = str(raw.get("tunnel_token") or "").strip()
    notify = raw.get("notify_on_failure", True)
    return Config(
        tunnel_token=token,
        external_hostname=parse_hostname(raw.get("external_hostname")),
        notify_on_failure=notify if isinstance(notify, bool) else True,
        run_parameters=_run_parameters(raw),
        log_level=parse_log_level(raw),
        origin_tls=_origin_tls(raw),
    )


def _origin_tls(raw: dict[str, object]) -> str:
    value = str(raw.get("origin_tls") or "auto").strip().lower()
    if value not in ORIGIN_TLS_MODES:
        raise ConfigError(f"origin_tls must be one of {', '.join(ORIGIN_TLS_MODES)}")
    return value
