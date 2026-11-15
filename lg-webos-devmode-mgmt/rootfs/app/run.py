#!/usr/bin/env python3
"""Home Assistant add-on: LG webOS Developer Mode session auto renew."""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

import maintenance
import redact
import tvssh
from safefile import atomic_write
from webui import (
    SECRET_PREFIX,
    PanelArgs,
    PanelError,
    PanelRouter,
    PanelServer,
    SecretError,
    SecretRefs,
    SetupView,
    action,
    badge,
    initial_options,
    stat,
    toggle,
    wait_for_valid_config,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path("/data/options.json")
PHONES_PATH = Path("/data/phones.json")
DATA_DIR = Path("/data")
WEB_DIR = Path(__file__).resolve().parent / "web"
LG_DEV_ENDPOINT = "https://developer.lge.com/secure/ResetDevModeSession.dev"
LG_CHECK_ENDPOINT = "https://developer.lge.com/secure/CheckDevModeSession.dev"
LG_HOST = "developer.lge.com"
SUPERVISOR_API = "http://supervisor"
SUPERVISOR_CORE_API = f"{SUPERVISOR_API}/core/api"
VERSION = os.environ.get("LG_RENEW_VERSION", "dev")

SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"  # noqa: S105 - env var name, not a secret
SUPERVISOR_TOKEN_ENV_CANDIDATES = (
    "SUPERVISOR_TOKEN",
    "SUPERVISOR_HASS_TOKEN",
    "HASSIO_TOKEN",
    "HOMEASSISTANT_TOKEN",
    "SUPERVISOR_API_TOKEN",
)

HTTP_TIMEOUT = 30
SUCCESS_RESULTS = {"success"}
INFO_MSG_CODES = {"GNL", "OK"}
SIGNIN_CODES = {"ERR_005"}  # "Check user session": LG no longer knows this token
SIGNIN_MSG_RE = re.compile(r"check\s+user\s+session", re.IGNORECASE)
SIGNIN_PAGE_RE = re.compile(r"type=[\"']?password|sign\s?in|log\s?in", re.IGNORECASE)
REMAINING_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})$")  # "999:13:25" = hours:minutes:seconds left
LOOP_SECONDS = 30
OPTIONS_CHECK_SECONDS = 30
PHONE_CHECK_SECONDS = 300
TV_SETTLE_SECONDS = 20  # after the TV turns on, before its SSH service answers
TV_RETRY_SECONDS = 30
TV_ATTEMPTS = 3
TV_OFF_STATES = frozenset({"off", "unavailable", "unknown", ""})
MEDIA_PLAYER_RE = re.compile(r"^media_player\.[a-z0-9_]+$")
MAX_PORT = 65535


class ConfigError(ValueError):
    """Raised when the add-on configuration is invalid."""


def slugify(value: str) -> str:
    """Lower-case id used for notifications and panel toggles."""

    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")


type LogLevel = Literal["trace", "debug", "info", "notice", "warning", "error", "fatal"]


@dataclass(frozen=True, slots=True)
class Session:
    """A single LG webOS device whose dev session is renewed."""

    name: str
    url: redact.Secret  # carries the session token: revealed only where the request is sent
    enabled: bool = True
    custom_url: bool = False
    check_url: redact.Secret | None = None  # LG's status address for the same token (None: not verifiable)
    token: redact.Secret | None = None  # the bare token, when the options give one LG can be asked about
    host: redact.Secret | None = None  # the TV's IP address: set to read the token from the TV over SSH
    port: int = tvssh.DEFAULT_PORT
    passphrase: redact.Secret | None = None  # decrypts the TV's SSH key
    media_player: str = ""  # the TV's entity in Home Assistant: a token read when it turns on
    private_key: redact.Secret | None = None  # the TV's SSH key from the options (else: uploaded or fetched)

    @property
    def notification_id(self) -> str:
        """Id used for notifications, panel switches and the status table."""

        return slugify(self.name)


type Outcome = Literal["ok", "signin", "failed"]
# What a notification is about: a renewal outcome, or something found on the TV.
type Reason = Literal[
    "failed", "signin", "changed", "changed_secret", "devmode_off", "keyserver", "passphrase", "key", "hostkey"
]


@dataclass(frozen=True, slots=True)
class RenewResult:
    """How one renewal ended: "signin" means LG no longer accepts the token."""

    outcome: Outcome
    remaining_hours: float | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


@dataclass(frozen=True, slots=True)
class Config:
    """Parsed add-on options (never changed in place: new options make a new Config)."""

    interval_hours: int
    retries: int
    notify_on_failure: bool
    sessions: tuple[Session, ...]
    maintenance_windows: tuple[maintenance.Window, ...] = ()
    log_level: LogLevel = "info"


TRACE = 5
NOTICE = 25
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


def setup_logging() -> None:
    """Configure logging to stdout so the supervisor captures it."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    redact.install()  # registered secrets (the Supervisor token...) never reach the log


_CONTAINER_ENV_DIRS = (Path("/run/s6/container_environment"), Path("/run/s6-container-env"))


def _merge_s6_environment() -> None:
    """Import s6 container environment vars into ``os.environ``.

    Supervisor injects its variables (``SUPERVISOR_TOKEN``, ``TZ``, ...) into
    the docker environment; s6-overlay captures them in
    ``/run/s6/container_environment`` but only passes them to services started
    with ``with-contenv``. Merge them here so they are visible to this process.
    """

    for directory in _CONTAINER_ENV_DIRS:
        if not directory.is_dir():
            continue
        for var in directory.iterdir():
            if not var.is_file() or not var.name:
                continue
            try:
                value = var.read_bytes().rstrip(b"\r\n\0").decode("utf-8", errors="replace")
            except OSError:
                continue
            if value:
                os.environ.setdefault(var.name, value)
    if hasattr(time, "tzset"):
        time.tzset()  # pick up the merged TZ so panel times are local


def _log_container_env_probe() -> None:
    """Log which names the container environment has (diagnostic; names only, never a value or a file's content)."""

    env_keys = sorted(os.environ)
    _LOGGER.debug("Container env var names (%d): %s", len(env_keys), ", ".join(env_keys[:80]))

    def _list_dir(path: str) -> None:
        if not os.path.isdir(path):
            return
        entries = sorted(os.listdir(path))
        _LOGGER.debug("%s entries (%d): %s", path, len(entries), ", ".join(entries[:60]))

    try:
        _list_dir("/run")
        _list_dir("/run/supervisor")
        for directory in _CONTAINER_ENV_DIRS:
            if directory.is_dir():
                names = ", ".join(sorted(p.name for p in directory.iterdir()))
                _LOGGER.debug("%s env var files: %s", directory, names or "(empty)")
    except OSError:
        _LOGGER.debug("Container probe failed", exc_info=True)


def load_supervisor_token() -> str:
    """Return the supervisor token from the container environment.

    The Home Assistant API proxy only permits requests carrying the add-on's
    token (``SUPERVISOR_TOKEN``). When it is unavailable, log the names of the
    supervisor-related environment variables that are present so the reason is
    visible in the add-on logs.
    """

    _merge_s6_environment()

    for name in SUPERVISOR_TOKEN_ENV_CANDIDATES:
        value = os.environ.get(name, "")
        if value:
            if name != SUPERVISOR_TOKEN_ENV:
                _LOGGER.debug("Using %s for the supervisor token", name)
            redact.register(value)  # never in a log line, whatever ends up logging it
            return value

    visible = ", ".join(sorted(key for key in os.environ if key.startswith(("SUPERVISOR_", "HASS", "HOMEASSISTANT"))))
    _LOGGER.warning(
        "No supervisor token found; scanned %s. Supervisor env vars present: %s",
        ", ".join(SUPERVISOR_TOKEN_ENV_CANDIDATES),
        visible or "(none)",
    )
    _log_container_env_probe()
    return ""


def build_sessions(raw_sessions: list[object]) -> tuple[Session, ...]:
    """Validate and normalize session options."""

    if not raw_sessions:
        raise ConfigError("No sessions configured; add at least one session with a token or URL")

    sessions: list[Session] = []
    for index, entry in enumerate(raw_sessions, start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"Session #{index} must be a mapping of options")

        name = str(entry.get("name") or f"tv-{index}").strip()
        if not name:
            raise ConfigError(f"Session #{index} has an empty name")

        token = str(entry.get("token") or "").strip()
        url = str(entry.get("url") or "").strip()

        if url:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme != "https" or not parts.hostname:
                raise ConfigError(f"Session '{name}': url must be an https:// address (it carries the session token)")
            found = urllib.parse.parse_qs(parts.query).get("sessionToken", [""])[0]
            token = found if parts.hostname == LG_HOST else ""  # only LG's own site can be asked for the status
        elif token:
            url = f"{LG_DEV_ENDPOINT}?sessionToken={urllib.parse.quote(token, safe='')}"
        else:
            raise ConfigError(f"Session '{name}' requires either a token or a full url")

        check_url = None
        if token:
            redact.register(token)  # never in a log line, with or without the rest of the address
            check_url = redact.Secret(f"{LG_CHECK_ENDPOINT}?sessionToken={urllib.parse.quote(token, safe='')}")

        enabled = entry.get("enabled")
        sessions.append(
            Session(
                name=name,
                url=redact.Secret(url),
                enabled=enabled if isinstance(enabled, bool) else True,
                custom_url=bool(entry.get("url")),
                check_url=check_url,
                token=redact.Secret(token) if token else None,
                **_tv_options(name, entry),
            )
        )

    return tuple(sessions)


class TvOptions(TypedDict, total=False):
    """The per-session options for reading the token from the TV."""

    host: redact.Secret
    port: int
    passphrase: redact.Secret
    media_player: str
    private_key: redact.Secret


def _tv_options(name: str, entry: dict[str, object]) -> TvOptions:
    """Validate ``host``, ``port``, ``passphrase`` and ``media_player`` of one session."""

    options: TvOptions = {}
    media_player = str(entry.get("media_player") or "").strip()
    if media_player:
        if not MEDIA_PLAYER_RE.match(media_player):
            raise ConfigError(
                f"Session '{name}': media_player must be an entity id such as media_player.living_room_tv"
            )
        options["media_player"] = media_player

    host = str(entry.get("host") or "").strip()
    private_key = str(entry.get("private_key") or "").strip()
    if not host:
        if private_key:
            raise ConfigError(f"Session '{name}': private_key needs host (the TV's IP address) and passphrase")
        if media_player:
            raise ConfigError(f"Session '{name}': media_player needs host (the TV's IP address) and passphrase")
        return options
    if not tvssh.valid_host(host):
        raise ConfigError(f"Session '{name}': host must be the TV's IP address")
    passphrase = str(entry.get("passphrase") or "").strip()
    if not passphrase:
        raise ConfigError(
            f"Session '{name}': passphrase is required when host is set "
            "(the 6-character code shown in the TV's Developer Mode app)"
        )
    raw_port = entry.get("port", tvssh.DEFAULT_PORT)
    port = int(raw_port) if isinstance(raw_port, int | str) and str(raw_port).isdigit() else 0
    if isinstance(raw_port, bool) or not 1 <= port <= MAX_PORT:
        raise ConfigError(f"Session '{name}': port must be a number from 1 to {MAX_PORT} (the TV's SSH port, 9922)")
    options["host"] = redact.Secret(host)
    options["port"] = port
    options["passphrase"] = redact.Secret(passphrase)
    if private_key:
        try:
            options["private_key"] = redact.Secret(tvssh.normalize_key(private_key).decode("utf-8"))
        except tvssh.KeyFormatError as err:
            raise ConfigError(f"Session '{name}': private_key: {err}") from err
    return options


def with_token(session: Session, token: str) -> Session:
    """``session`` renewing with ``token`` instead of the one in the options."""

    quoted = urllib.parse.quote(token, safe="")
    if session.custom_url:
        parts = urllib.parse.urlsplit(session.url.reveal())
        query = [(k, token if k == "sessionToken" else v) for k, v in urllib.parse.parse_qsl(parts.query)]
        url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    else:
        url = f"{LG_DEV_ENDPOINT}?sessionToken={quoted}"
    return dataclasses.replace(
        session,
        url=redact.Secret(url),
        check_url=redact.Secret(f"{LG_CHECK_ENDPOINT}?sessionToken={quoted}"),
        token=redact.Secret(token),
    )


MOBILE_TARGET_PREFIX = "mobile_app_"


MIN_INTERVAL_HOURS = 48


def _int(raw: dict[str, object], key: str, default: int, low: int, high: int | None = None) -> int:
    """A whole-number option, kept at or above ``low`` and, when given, at or below ``high``."""

    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    value = max(value, low)
    return value if high is None else min(value, high)


def load_config(raw_config: dict[str, object]) -> Config:
    """Validate raw options and produce a Config."""

    sessions_raw = raw_config.get("sessions")
    sessions = build_sessions(sessions_raw if isinstance(sessions_raw, list) else [])

    try:
        windows = maintenance.parse_windows(raw_config.get("maintenance_windows"))
    except maintenance.MaintenanceConfigError as err:
        raise ConfigError(f"maintenance_windows: {err}") from err

    notify = raw_config.get("notify_on_failure")
    interval_hours = _int(raw_config, "interval_hours", MIN_INTERVAL_HOURS, MIN_INTERVAL_HOURS)
    saved = raw_config.get("interval_hours")
    if isinstance(saved, int) and not isinstance(saved, bool) and saved < interval_hours:
        _LOGGER.warning("interval_hours is %s, below the minimum: renewing every %s h instead", saved, interval_hours)
    return Config(
        interval_hours=interval_hours,
        retries=_int(raw_config, "retries", 3, 0, 10),
        notify_on_failure=notify if isinstance(notify, bool) else True,
        sessions=sessions,
        maintenance_windows=tuple(windows),
        log_level=parse_log_level(raw_config),
    )


def load_options() -> dict[str, object]:
    """Read and parse the supervisor options file."""

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"Configuration file not found at {CONFIG_PATH}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"Invalid JSON in {CONFIG_PATH}: {err}") from err
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration root in {CONFIG_PATH} must be a mapping")

    return raw


def _supervisor_request(supervisor_token: str, path: str, body: object | None = None) -> object:
    """JSON request to the Supervisor API (``path`` starts with '/')."""

    request = urllib.request.Request(  # noqa: S310 - internal supervisor API
        f"{SUPERVISOR_API}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method="GET" if body is None else "POST",
        headers={"Authorization": f"Bearer {supervisor_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - internal supervisor API
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else None


def fetch_options(supervisor_token: str) -> dict[str, object]:
    """Current saved options (including edits made in the Configuration tab)."""

    reply = _supervisor_request(supervisor_token, "/addons/self/info")
    data = reply.get("data") if isinstance(reply, dict) else None
    options = data.get("options") if isinstance(data, dict) else None
    if not isinstance(options, dict):
        raise ConfigError("Supervisor returned no add-on options")
    return options


def write_options(supervisor_token: str, options: dict[str, object]) -> None:
    """Replace the saved options; the payload must be wrapped as {"options": ...}."""

    _supervisor_request(supervisor_token, "/addons/self/options", {"options": options})


def _payload(body: str) -> dict[str, object] | None:
    """LG's JSON answer, or None when the body is empty or something else (e.g. a web page)."""

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _wants_signin(payload: dict[str, object]) -> bool:
    """Whether LG's answer says the session token is no longer known (a new sign-in on the TV)."""

    return str(payload.get("errorCode") or "") in SIGNIN_CODES or bool(
        SIGNIN_MSG_RE.search(str(payload.get("errorMsg") or ""))
    )


def _succeeded(payload: dict[str, object]) -> bool:
    return str(payload.get("result") or "").lower() in SUCCESS_RESULTS and str(payload.get("errorCode") or "") == "200"


def handle_response(body: str, status: int, session_name: str, *, redirected: bool = False) -> RenewResult:
    """Interpret LG's answer to a renewal (the body is never logged: it may repeat the token)."""

    payload = _payload(body)
    if payload is None:
        if redirected or SIGNIN_PAGE_RE.search(body):
            _LOGGER.error("Renewal for '%s' was answered with a sign-in page (HTTP %s)", session_name, status)
            return RenewResult("signin")
        _LOGGER.error("Renewal for '%s' returned an answer that is not LG's JSON (HTTP %s)", session_name, status)
        return RenewResult("failed")

    error_msg = str(payload.get("errorMsg") or "")
    if _succeeded(payload):
        if error_msg and error_msg not in INFO_MSG_CODES:
            _LOGGER.info("Renewal for '%s' succeeded: %s", session_name, error_msg)
        else:
            _LOGGER.info("Renewal for '%s' succeeded", session_name)
        return RenewResult("ok")

    _LOGGER.error(
        "Renewal for '%s' failed per LG API: result=%s errorCode=%s errorMsg=%s",
        session_name,
        str(payload.get("result") or "").lower(),
        str(payload.get("errorCode") or ""),
        error_msg,
    )
    if _wants_signin(payload):
        _LOGGER.error("LG no longer accepts the token of '%s': sign in on the TV and save its new token", session_name)
        return RenewResult("signin")
    return RenewResult("failed")


def remaining_hours(text: str) -> float | None:
    """'999:13:25' -> 999.22 (None when LG's text is not a time)."""

    match = REMAINING_RE.match(text.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours + minutes / 60 + seconds / 3600


class VendorMaintenanceError(Exception):
    """LG's developer site answered with a maintenance page or HTTP 503."""


def _get(url: redact.Secret) -> tuple[int, str, bool]:
    """GET an LG address: status, body and whether LG sent the request somewhere else.

    Raises VendorMaintenanceError for a maintenance answer; other errors are urllib's.
    """

    target = url.reveal()
    try:
        with urllib.request.urlopen(target, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - https only (build_sessions)
            body = response.read().decode("utf-8", errors="replace")
            status = int(response.status)
            final = urllib.parse.urlsplit(str(response.geturl()))
    except urllib.error.HTTPError as err:
        if maintenance.looks_like_maintenance(err.code):
            raise VendorMaintenanceError(f"LG developer site is under maintenance (HTTP {err.code})") from err
        raise
    if maintenance.looks_like_maintenance(status, body):
        raise VendorMaintenanceError(f"LG developer site is under maintenance (HTTP {status})")
    asked = urllib.parse.urlsplit(target)
    return status, body, (asked.hostname, asked.path) != (final.hostname, final.path)


def check_session(session: Session) -> RenewResult:
    """Ask LG whether the session is alive and how long it has left.

    Called after a renewal LG accepted. When the status cannot be read (no
    status address, network error, maintenance, an answer that is not JSON) the
    renewal stays a success: only a clear "no such session" turns it around.
    """

    if session.check_url is None:
        return RenewResult("ok")
    try:
        status, body, redirected = _get(session.check_url)
    except (VendorMaintenanceError, urllib.error.URLError, OSError):
        _LOGGER.info("Could not read the remaining time of '%s' from LG", session.name)
        return RenewResult("ok")

    payload = _payload(body)
    if payload is None:
        if redirected or SIGNIN_PAGE_RE.search(body):
            _LOGGER.error("LG asks for a sign-in when checking '%s' (HTTP %s)", session.name, status)
            return RenewResult("signin")
        _LOGGER.debug("LG's status answer for '%s' is not JSON (HTTP %s)", session.name, status)
        return RenewResult("ok")
    if not _succeeded(payload):
        _LOGGER.error(
            "LG does not know the session of '%s' (errorCode=%s): sign in on the TV and save its new token",
            session.name,
            str(payload.get("errorCode") or ""),
        )
        return RenewResult("signin")

    hours = remaining_hours(str(payload.get("errorMsg") or ""))
    if hours is not None:
        _LOGGER.info("Session '%s' has %d h left", session.name, int(hours))
    return RenewResult("ok", hours)


def renew_session(session: Session) -> RenewResult:
    """Renew one dev session, then verify it with LG (raises VendorMaintenanceError)."""

    _LOGGER.info("Renewing dev session '%s'", session.name)
    try:
        status, body, redirected = _get(session.url)
    except urllib.error.HTTPError as err:
        _LOGGER.exception("Renewal for '%s' failed: HTTP %s %s", session.name, err.code, err.reason)
        return RenewResult("failed")
    except urllib.error.URLError as err:
        _LOGGER.exception("Renewal for '%s' failed: %s", session.name, err.reason)
        return RenewResult("failed")
    except OSError:
        _LOGGER.exception("Renewal for '%s' failed", session.name)
        return RenewResult("failed")
    result = handle_response(body, status, session.name, redirected=redirected)
    return check_session(session) if result.ok else result


def renew_with_retries(
    session: Session,
    retries: int,
    backoff_seconds: int = 30,
    sleep: Callable[[float], object] = time.sleep,
) -> RenewResult:
    """Renew a session, retrying with exponential backoff on failure.

    A "signin" answer is final: a token LG no longer knows does not come back.
    """

    result = RenewResult("failed")
    for attempt in range(retries + 1):
        result = renew_session(session)
        if result.outcome != "failed":
            return result
        if attempt < retries:
            wait = backoff_seconds * (2**attempt)
            _LOGGER.warning(
                "Renewal for '%s' failed; retrying in %s s (attempt %d/%d)",
                session.name,
                wait,
                attempt + 1,
                retries,
            )
            sleep(wait)
    return result


def discover_mobile_targets(supervisor_token: str) -> list[str] | None:
    """Discover notify.mobile_app_* targets from existing Home Assistant services.

    Every Companion app linked to this Home Assistant registers a
    ``notify.mobile_app_<device>`` service; each such service becomes a target.
    Returns None when the API is unreachable (so the known list is kept).
    """

    if not supervisor_token:
        return None

    try:
        services = _supervisor_request(supervisor_token, "/core/api/services")
    except (urllib.error.URLError, OSError, ValueError):
        _LOGGER.warning("Could not list mobile-app notification targets; keeping the known phones")
        return None

    if not isinstance(services, list):
        _LOGGER.warning("Unexpected /services response (expected a list)")
        return None

    notify_targets = {
        str(name)
        for entry in services
        if isinstance(entry, dict) and entry.get("domain") == "notify"
        for name in (entry.get("services") or {})
    }
    return sorted(t for t in notify_targets if t.startswith(MOBILE_TARGET_PREFIX))


def load_phone_prefs(path: Path = PHONES_PATH) -> dict[str, bool]:
    """Per-phone notification switches kept in /data (phone name -> notify)."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _LOGGER.warning("Phone settings file is unreadable; every phone is notified")
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(name): value for name, value in raw.items() if isinstance(value, bool)}


def save_phone_prefs(prefs: dict[str, bool], path: Path = PHONES_PATH) -> None:
    """Write the phone switches atomically."""

    try:
        atomic_write(path, json.dumps(prefs, sort_keys=True))
    except OSError:
        _LOGGER.exception("Could not save phone settings")


def overrides_from_options(raw_options: dict[str, object]) -> dict[str, bool]:
    """Phone switches from the retired ``notification_overrides`` option (migration)."""

    rows = raw_options.get("notification_overrides")
    prefs: dict[str, bool] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("name") or "").strip():
            prefs[str(row["name"]).strip()] = bool(row.get("notify", True))
    return prefs


def merge_discovered(prefs: dict[str, bool], discovered: list[str]) -> dict[str, bool]:
    """Switches for exactly the linked phones: new phones on, unlinked phones dropped."""

    return {name: prefs.get(name, True) for name in discovered}


def effective_mobile_targets(discovered: list[str], prefs: dict[str, bool]) -> list[str]:
    """Return linked phones that are not switched off."""

    return [target for target in discovered if prefs.get(target, True)]


SIGNIN_STEPS = (
    "Open the Developer Mode app on the TV, sign in with your LG account email and password, "
    "switch Dev Mode on, then read the TV's new token and save it in the app's Configuration tab."
)


def notification_text(session: Session, reason: Reason) -> tuple[str, str]:
    """Title and message of the notification for ``reason``."""

    name = session.name
    texts: dict[Reason, tuple[str, str]] = {
        "failed": (
            "LG webOS DevMode renewal failed",
            (
                f"The LG webOS Developer Mode session '{name}' could not be renewed "
                "after exhausting all retries. Renew it manually or the dev session may expire."
            ),
        ),
        "signin": (
            "LG webOS DevMode needs a new sign-in",
            f"LG no longer accepts the Developer Mode session token of '{name}'. {SIGNIN_STEPS}",
        ),
        "changed": (
            "LG webOS DevMode token changed",
            (
                f"The Developer Mode session token of '{name}' changed on the TV. "
                "The app read the new one from the TV, saved it and now renews with it."
            ),
        ),
        "changed_secret": (
            "LG webOS DevMode token changed",
            (
                f"The Developer Mode session token of '{name}' changed on the TV. The app renews with the new one "
                "until it restarts, but cannot save it: the option is a !secret reference or a custom URL. "
                "Update the value there."
            ),
        ),
        "devmode_off": (
            "LG webOS Developer Mode is off",
            f"The TV '{name}' has no Developer Mode session token: Developer Mode is switched off. {SIGNIN_STEPS}",
        ),
        "keyserver": (
            "LG webOS DevMode cannot read the TV",
            (
                f"The TV '{name}' has no SSH key the app can use, and its key server is off. Upload the TV's "
                "private key on its card in the LG DevMode panel, or switch Key Server on in the TV's "
                "Developer Mode app so the app can fetch it."
            ),
        ),
        "passphrase": (
            "LG webOS DevMode cannot read the TV",
            (
                f"The TV '{name}' refused its own SSH key: the passphrase option is wrong. Enter the 6-character "
                "code shown in the TV's Developer Mode app in the app's Configuration tab."
            ),
        ),
        "key": (
            "LG webOS DevMode cannot read the TV",
            (
                f"The TV '{name}' refused the SSH key from the private_key option. The TV issues a new key when "
                "Developer Mode is switched on again: save the TV's current private key, or check the passphrase."
            ),
        ),
        "hostkey": (
            "LG webOS DevMode: the TV's identity changed",
            (
                f"The SSH host key of the TV '{name}' is not the one the app saw first, so the app did not connect. "
                "If the TV was reset or replaced, see 'changed host key' in the app's documentation."
            ),
        ),
    }
    return texts[reason]


def notify_failure(session: Session, targets: list[str], supervisor_token: str, reason: Reason = "failed") -> None:
    """Notify Home Assistant about a failed renewal or something found on the TV.

    Routes to the HA notify service (e.g. the mobile app) when notification
    targets are configured; otherwise falls back to a persistent notification.
    """

    if not supervisor_token:
        _LOGGER.warning("Cannot notify for '%s': no supervisor token available", session.name)
        return

    title, message = notification_text(session, reason)

    if targets:
        for target in targets:
            try:
                _supervisor_request(
                    supervisor_token,
                    f"/core/api/services/notify/{urllib.parse.quote(target, safe='')}",
                    {"title": title, "message": message},
                )
                _LOGGER.info("Failure notification sent to '%s' for '%s'", target, session.name)
            except (urllib.error.URLError, OSError, ValueError):
                _LOGGER.exception("Failed to send notification to '%s' for '%s'", target, session.name)
        return

    try:
        _supervisor_request(
            supervisor_token,
            "/core/api/services/persistent_notification/create",
            {"notification_id": session.notification_id, "title": title, "message": message},
        )
        _LOGGER.info("Failure notification sent for '%s'", session.name)
    except (urllib.error.URLError, OSError, ValueError):
        _LOGGER.exception("Failed to send notification for '%s'", session.name)


# -- App: scheduler, panel and live options ---------------------------------------
@dataclass(slots=True)
class SessionStatus:
    """Last known renewal outcome of one TV (wall-clock epoch seconds)."""

    last_attempt: float | None = None
    last_success: float | None = None
    last_ok: bool | None = None
    running: bool = False
    maintenance: bool = False
    needs_signin: bool = False  # LG no longer accepts the token
    remaining_hours: float | None = None  # as LG reported after the last good renewal
    tv_state: str = ""  # outcome of the last token read from the TV, as shown in the panel
    tv_checked: float | None = None  # when the token was last read from the TV
    tv_alert: Reason | None = None  # a problem on the TV the user has been told about


def _clock(epoch: float | None) -> str | None:
    return None if epoch is None else time.strftime("%d %b %H:%M", time.localtime(epoch))


def apply_toggle(options: dict[str, object], toggle_id: str, value: bool) -> None:
    """Write one panel switch into a raw options dict (mutates ``options``)."""

    kind, _, rest = toggle_id.partition(":")
    if kind == "notify_on_failure" and not rest:
        options["notify_on_failure"] = value
        return
    if kind == "session":
        key, _, field_name = rest.rpartition(":")
        matched = False
        if field_name == "enabled":
            rows = options.get("sessions")
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and slugify(str(row.get("name") or "")) == key:
                    row["enabled"] = value
                    matched = True
        if matched:
            return
        raise PanelError("That TV no longer exists; reload the page")
    raise PanelError("Unknown setting")


class App:
    """Renews on a schedule, serves the ingress panel and applies option changes live."""

    def __init__(
        self,
        config: Config,
        raw_options: dict[str, object],
        supervisor_token: str,
        phones_path: Path = PHONES_PATH,
        data_dir: Path = DATA_DIR,
    ) -> None:
        self.config = config
        self.raw_options = raw_options  # as stored: !secret references, never their values
        self.secrets = SecretRefs()
        self.token = supervisor_token
        self.phones_path = phones_path
        self.phones: dict[str, bool] = load_phone_prefs(phones_path)
        self.discovered: list[str] = list(self.phones)
        self.next_phone_check = 0.0
        self.status: dict[str, SessionStatus] = {}
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None
        self.queue: list[str | None] = []  # session keys to renew now (None = all)
        self.next_run = time.time()  # first renewal right away
        self.last_scheduled: float | None = None
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.backoff = maintenance.Backoff()
        self.data_dir = data_dir
        self.tv_tokens: dict[str, redact.Secret] = {}  # tokens read from the TV (memory only, never on disk)
        self.tv_on: dict[str, bool] = {}  # last power state seen per session
        self.tv_due: dict[str, tuple[float, int]] = {}  # session -> (when to read the TV, attempts left)

    # -- options ------------------------------------------------------------
    def view_config(self) -> Config:
        """Config the panel should show (a saved-but-not-yet-applied change wins)."""

        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        """Switch to new options in place (main thread)."""

        if config.log_level != self.config.log_level:
            _LOGGER.info("Log level set to %s", config.log_level)  # before applying: may hide info
            apply_log_level(config.log_level)
        if config.interval_hours != self.config.interval_hours and self.last_scheduled is not None:
            self.next_run = self.last_scheduled + config.interval_hours * 3600
        newly_enabled = [
            s.name
            for s in config.sessions
            if s.enabled and not any(o.notification_id == s.notification_id and o.enabled for o in self.config.sessions)
        ]
        self.config = config
        self.raw_options = raw
        _LOGGER.info(
            "Applied updated options: %d of %d TV(s) enabled, interval %s h",
            sum(s.enabled for s in config.sessions),
            len(config.sessions),
            config.interval_hours,
        )
        if newly_enabled:
            _LOGGER.info("Auto renew switched on for: %s", ", ".join(newly_enabled))

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel; applied on the next loop pass."""

        with self.options_lock:
            try:
                base = fetch_options(self.token)
            except (urllib.error.URLError, OSError, ValueError, ConfigError) as err:
                # Never fall back to the start-up copy: it holds resolved !secret values.
                raise PanelError("Home Assistant is not reachable; try again") from err
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(self.secrets.resolve_options(raw))
            except (ConfigError, SecretError) as err:
                raise PanelError(str(err)) from err
            try:
                write_options(self.token, raw)
            except (urllib.error.URLError, OSError, ValueError) as err:
                raise PanelError("Home Assistant rejected the change; see the app log") from err
            self.pending = (raw, config)
        self.wake.set()

    def check_options(self) -> None:
        """Pick up edits made in the Configuration tab."""

        try:
            raw = fetch_options(self.token)
        except (urllib.error.URLError, OSError, ValueError, ConfigError):
            return
        if raw == self.raw_options:
            return
        try:
            config = load_config(self.secrets.resolve_options(raw))
        except (ConfigError, SecretError) as err:
            _LOGGER.error("Ignoring updated options until they are fixed: %s", err)  # noqa: TRY400 - no traceback needed
            self.raw_options = raw
            return
        self.apply_config(raw, config)

    # -- phones -------------------------------------------------------------
    def migrate_overrides(self) -> None:
        """Move switches from the retired ``notification_overrides`` option into /data.

        The key has to be looked up in the options the Supervisor has stored
        (``/addons/self/info``): ``/data/options.json`` is already filtered to
        the schema, so the key never shows up there, yet the stored copy makes
        the Supervisor warn about it on every options check.
        """

        try:
            stored = fetch_options(self.token)
        except (urllib.error.URLError, OSError, ValueError, ConfigError):
            return  # the start-up copy holds resolved secrets; never write it back
        if "notification_overrides" not in stored:
            return
        old = overrides_from_options(stored)
        for name, notify in old.items():
            self.phones.setdefault(name, notify)
        save_phone_prefs(self.phones, self.phones_path)
        raw = {k: v for k, v in stored.items() if k != "notification_overrides"}
        try:
            write_options(self.token, raw)
            self.raw_options = raw
            _LOGGER.info("Moved %d phone switch(es) out of the Configuration tab", len(old))
        except (urllib.error.URLError, OSError, ValueError):
            _LOGGER.warning("Could not remove notification_overrides from the options; it is ignored")

    def refresh_phones(self) -> None:
        """Re-list the phones linked to Home Assistant (new phones are notified by default)."""

        found = discover_mobile_targets(self.token)
        if found is None:
            return
        with self.options_lock:
            merged = merge_discovered(self.phones, found)
            added = sorted(set(merged) - set(self.phones))
            removed = sorted(set(self.phones) - set(merged))
            self.discovered = found
            if merged != self.phones:
                self.phones = merged
                save_phone_prefs(merged, self.phones_path)
        if added:
            _LOGGER.info("Found phone(s): %s", ", ".join(added))
        if removed:
            _LOGGER.info("Phone(s) no longer linked: %s", ", ".join(removed))
        if added or removed:
            targets = effective_mobile_targets(self.discovered, self.phones)
            _LOGGER.info("Notifications go to %d phone(s): %s", len(targets), ", ".join(targets) or "-")

    def set_phone(self, name: str, value: bool) -> None:
        """Panel switch for one phone (kept in /data, applies immediately)."""

        with self.options_lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            save_phone_prefs(self.phones, self.phones_path)

    # -- renewals -----------------------------------------------------------
    def renew(self, sessions: Sequence[Session]) -> bool:
        """Renew the given sessions, recording status and notifying on failure.

        Returns False when LG's site answered with a maintenance page: the
        remaining sessions are skipped and no failure is reported.
        """

        for configured in sessions:
            if self.stopping.is_set():
                return True
            session = self.effective(configured)
            status = self.status.setdefault(session.notification_id, SessionStatus())
            status.running = True
            status.last_attempt = time.time()
            try:
                result = renew_with_retries(session, self.config.retries, sleep=self.stopping.wait)
            except VendorMaintenanceError as err:
                status.running = False
                for other in sessions:
                    self.status.setdefault(other.notification_id, SessionStatus()).maintenance = True
                self._retry_after_maintenance(str(err))
                return False
            status.running = False
            status.maintenance = False
            already_asked = status.needs_signin  # one "sign in" alert, not one per interval
            status.last_ok = result.ok
            status.needs_signin = result.outcome == "signin"
            status.remaining_hours = result.remaining_hours
            if result.ok:
                status.last_success = time.time()
            elif self.config.notify_on_failure and not (status.needs_signin and already_asked):
                targets = effective_mobile_targets(self.discovered, self.phones)
                notify_failure(session, targets, self.token, "signin" if status.needs_signin else "failed")
        self.backoff.reset()
        return True

    # -- the token on the TV ----------------------------------------------------
    def effective(self, session: Session) -> Session:
        """``session`` with the token last read from the TV, when that differs from the options."""

        override = self.tv_tokens.get(session.notification_id)
        if override is None:
            return session
        if session.token == override:
            del self.tv_tokens[session.notification_id]  # the options caught up
            return session
        return with_token(session, override.reveal())

    def _notify(self, session: Session, reason: Reason) -> None:
        if self.config.notify_on_failure:
            notify_failure(session, effective_mobile_targets(self.discovered, self.phones), self.token, reason)

    def _tv_problem(self, session: Session, status: SessionStatus, reason: Reason, state: str) -> None:
        """Record a problem found on the TV; the user is told once, not on every check."""

        status.tv_state = state
        if status.tv_alert != reason:
            status.tv_alert = reason
            self._notify(session, reason)

    def watch_power(self) -> None:
        """Queue a token read for every TV that Home Assistant reports as just turned on."""

        for session in self.config.sessions:
            if not (session.enabled and session.host and session.media_player):
                continue
            try:
                reply = _supervisor_request(self.token, f"/core/api/states/{session.media_player}")
            except (urllib.error.URLError, OSError, ValueError):
                continue  # Home Assistant is restarting or the entity is gone: no change seen
            state = str(reply.get("state") or "") if isinstance(reply, dict) else ""
            is_on = state not in TV_OFF_STATES
            was_on = self.tv_on.get(session.notification_id, False)
            self.tv_on[session.notification_id] = is_on
            if is_on and not was_on:
                _LOGGER.info("'%s' turned on; reading its token shortly", session.name)
                self.tv_due[session.notification_id] = (time.monotonic() + TV_SETTLE_SECONDS, TV_ATTEMPTS)

    def run_tv_checks(self) -> None:
        """Read the token from every TV whose check is due (retried while the TV is not reachable yet)."""

        now = time.monotonic()
        for key, (due, attempts) in list(self.tv_due.items()):
            if now < due:
                continue
            del self.tv_due[key]
            session = next((s for s in self.config.sessions if s.notification_id == key and s.host), None)
            if session is None:
                continue
            if not self.verify_tv(session, renew_after=True) and attempts > 1:
                self.tv_due[key] = (time.monotonic() + TV_RETRY_SECONDS, attempts - 1)

    def _known_hosts(self, session: Session) -> Path:
        """Where the SSH host key of this TV is pinned (one file per TV)."""

        return self.data_dir / "keys" / f"{session.notification_id}.known_hosts"

    def _tv(self, session: Session) -> tvssh.Tv | None:
        """How to reach this session's TV (None without the host option)."""

        if session.host is None or session.passphrase is None:
            return None
        return tvssh.Tv(
            host=session.host,
            port=session.port,
            passphrase=session.passphrase,
            key_path=self.data_dir / "keys" / session.notification_id,
            known_hosts=self._known_hosts(session),
        )

    def _key_text(self, session: Session) -> str:
        """Where this TV's SSH key comes from, for the panel."""

        tv = self._tv(session)
        if session.private_key is not None:
            return "From options"
        source = tvssh.key_source(tv) if tv else ""
        if source == "upload" and tv is not None:
            return f"Uploaded {_clock(tv.key_path.stat().st_mtime)}"
        return "From TV key server" if source else "None yet"

    def _tv_action(self, action_id: str, args: PanelArgs) -> str:
        """The panel buttons about the TV itself: check it, trust it again, upload its key."""

        target = args.ident("session")
        match = next((s for s in self.config.sessions if s.notification_id == target), None)
        tv = self._tv(match) if match else None
        if match is None or tv is None:
            raise PanelError("That TV no longer exists or has no host option; reload the page")
        note = "Reading"
        if action_id == "trust_tv":
            self._known_hosts(match).unlink(missing_ok=True)  # pinned again at the next contact
            _LOGGER.warning("Forgot the pinned SSH host key of '%s' on request", match.name)
        elif action_id == "upload_key":
            if match.private_key is not None:
                raise PanelError("The private_key option is set, so it is used instead; remove it to upload a key")
            files = args.files()
            if len(files) != 1:
                raise PanelError("Choose one file: the TV's private key")
            try:
                tvssh.store_key(tv, tvssh.normalize_key(files[0][1]), "upload")
            except tvssh.KeyFormatError as err:
                raise PanelError(f"Not saved: {err}") from err
            except OSError as err:
                raise PanelError("The key could not be saved; see the app log") from err
            _LOGGER.info("A new SSH key was uploaded for '%s'", match.name)
            status = self.status.setdefault(match.notification_id, SessionStatus())
            status.tv_alert = None
            status.tv_state = "Key uploaded"
            note = "Key saved. Reading"
        self.tv_due[match.notification_id] = (time.monotonic(), 1)
        self.wake.set()
        return f"{note} the token from {match.name}..."

    def _read_tv(self, configured: Session, status: SessionStatus) -> tuple[str | None, bool]:
        """The token on the TV (None when there is none to use) and whether the TV was reachable."""

        tv = self._tv(configured)
        if tv is None:
            return None, True
        own_key = configured.private_key
        problems: dict[type[tvssh.TvError], tuple[Reason, str, str]] = {
            tvssh.NoTokenError: ("devmode_off", "Dev Mode off on TV", "has no session token: Developer Mode is off"),
            tvssh.KeyServerError: ("keyserver", "Turn on Key Server", "key cannot be fetched: switch Key Server on"),
            tvssh.KeyRejectedError: (
                ("key", "Replace the SSH key", "refused the SSH key from the options")
                if own_key
                else ("passphrase", "Check the passphrase", "refused its own SSH key: check passphrase")
            ),
            tvssh.HostKeyChangedError: (
                "hostkey",
                "TV host key changed",
                "has a different SSH host key; not connecting",
            ),
        }
        try:
            if own_key:
                tvssh.store_key(tv, own_key.reveal().encode("utf-8"), "option")
            return tvssh.read_token_with_key(tv, own_key=own_key is not None), True
        except tvssh.UnreachableError:
            _LOGGER.info("'%s' is not reachable over SSH (off or asleep)", configured.name)
            status.tv_state = "TV not reachable"
            return None, False
        except (tvssh.TvError, OSError) as err:
            known = problems.get(type(err)) if isinstance(err, tvssh.TvError) else None
            if known is None:
                _LOGGER.warning("Could not read the token of '%s' from the TV: %s", configured.name, err)
                status.tv_state = "Check failed"
            else:
                reason, state, log_text = known
                _LOGGER.error("TV '%s' %s", configured.name, log_text)  # noqa: TRY400 - no traceback needed
                self._tv_problem(configured, status, reason, state)
            return None, True

    def verify_tv(self, configured: Session, *, renew_after: bool) -> bool:
        """Read the token from the TV, compare it with the saved one and check it with LG.

        Returns False only when the TV could not be reached (worth another try).
        """

        status = self.status.setdefault(configured.notification_id, SessionStatus())
        token, reachable = self._read_tv(configured, status)
        if token is None:
            return reachable

        status.tv_checked = time.time()
        status.tv_alert = None
        session = self.effective(configured)
        same = session.token is not None and session.token == redact.Secret(token)
        candidate = session if same else with_token(session, token)
        in_window = self.maintenance_until() is not None  # no calls to LG inside a maintenance window
        result = RenewResult("ok") if in_window else check_session(candidate)

        if result.outcome == "signin":
            _LOGGER.error("LG does not accept the token '%s' holds", configured.name)
            status.tv_state = "Not accepted by LG"
            status.last_ok = False
            if not status.needs_signin:
                status.needs_signin = True
                self._notify(configured, "signin")
            return True
        if result.remaining_hours is not None:
            status.remaining_hours = result.remaining_hours
        if same:
            _LOGGER.info("'%s' holds the saved token", configured.name)
            status.tv_state = "Same as saved"
            return True
        if session.token is None:  # a custom URL outside LG: no saved token to compare with or replace
            status.tv_state = "Read from TV"
            return True

        _LOGGER.warning("The token of '%s' changed on the TV; using the new one", configured.name)
        self.tv_tokens[configured.notification_id] = redact.Secret(token)
        saved = self.adopt_token(configured, token)
        status.tv_state = "Updated from TV" if saved else "Changed on TV (not saved)"
        status.needs_signin = False
        self._notify(configured, "changed" if saved else "changed_secret")
        if renew_after and not in_window:
            with self.options_lock:
                self.queue.append(configured.notification_id)
        return True

    def adopt_token(self, session: Session, token: str) -> bool:
        """Save the TV's token as the session's ``token`` option; False when it cannot be saved.

        A ``!secret`` reference or a custom ``url`` is never overwritten: the
        token is then used from memory only.
        """

        class NotWritableError(Exception):
            pass

        def mutate(options: dict[str, object]) -> None:
            rows = options.get("sessions")
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or slugify(str(row.get("name") or "")) != session.notification_id:
                    continue
                current = row.get("token")
                if row.get("url") or not isinstance(current, str) or current.startswith(SECRET_PREFIX):
                    raise NotWritableError
                row["token"] = token
                return
            raise NotWritableError

        try:
            self.update_options(mutate)
        except NotWritableError:
            return False
        except PanelError as err:
            _LOGGER.warning("Could not save the new token of '%s': %s", session.name, err)
            return False
        return True

    def maintenance_until(self) -> datetime | None:
        """End of the current LG maintenance window (Home Assistant's timezone), or None."""

        return maintenance.window_end(list(self.config.maintenance_windows), datetime.now().astimezone())

    def _retry_after_maintenance(self, reason: str) -> None:
        delay = self.backoff.next_delay()
        self.next_run = time.time() + delay  # re-renewing early is harmless
        _LOGGER.warning("%s; retrying at %s", reason, time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(self.next_run)))

    def _tick(self) -> None:
        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        if time.monotonic() >= self.next_options_check:
            self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
            self.check_options()
        if time.monotonic() >= self.next_phone_check:
            self.next_phone_check = time.monotonic() + PHONE_CHECK_SECONDS
            self.refresh_phones()
        self.watch_power()
        self.run_tv_checks()

        with self.options_lock:
            queued, self.queue = self.queue, []
        if queued:
            wanted = None if None in queued else set(queued)
            self.renew([s for s in self.config.sessions if wanted is None or s.notification_id in wanted])

        if time.time() >= self.next_run:
            until = self.maintenance_until()
            if until is not None:
                self.next_run = until.timestamp() + maintenance.resume_jitter_seconds()
                _LOGGER.info(
                    "LG maintenance window (%s): renewal moved to %s",
                    maintenance.describe(list(self.config.maintenance_windows)),
                    time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(self.next_run)),
                )
                return
            enabled = [s for s in self.config.sessions if s.enabled]
            _LOGGER.info("Scheduled renewal of %d TV(s)", len(enabled))
            for session in enabled:  # renew with the token the TV holds now, when it can be read
                if session.host and session.notification_id not in self.tv_due:
                    self.verify_tv(session, renew_after=False)
            if not self.renew(enabled):
                return  # LG maintenance: _retry_after_maintenance set next_run
            self.last_scheduled = time.time()
            self.next_run = self.last_scheduled + self.config.interval_hours * 3600
            _LOGGER.info("Next renewal at %s", time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(self.next_run)))

    def run(self) -> None:
        """Run the scheduling loop until stopped (the panel server is owned by ``main``)."""

        while not self.stopping.is_set():
            self.wake.clear()
            self._tick()
            self.wake.wait(LOOP_SECONDS)

    # -- panel (HTTP thread) --------------------------------------------------
    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        """Persist and apply one panel switch."""

        kind, _, name = toggle_id.partition(":")
        if kind == "phone" and name:
            self.set_phone(name, value)
            return
        self.update_options(lambda raw: apply_toggle(raw, toggle_id, value))

    def panel_action(self, action_id: str, args: PanelArgs) -> str | None:
        """Queue a renewal from a panel button."""

        if action_id == "renew_all":
            target: str | None = None
            label = "all TVs"
        elif action_id in {"check_tv", "trust_tv", "upload_key"}:
            return self._tv_action(action_id, args)
        elif action_id == "renew":
            target = args.ident("session")
            match = next((s for s in self.config.sessions if s.notification_id == target), None)
            if match is None:
                raise PanelError("That TV no longer exists; reload the page")
            label = match.name
        else:
            raise PanelError("Unknown action")
        until = self.maintenance_until()
        if until is not None:
            raise PanelError(
                f"LG's developer site is in its maintenance window until {until:%H:%M}; try again after that"
            )
        with self.options_lock:
            self.queue.append(target)
        self.wake.set()
        return f"Renewing {label}..."

    def _session_card(self, session: Session) -> dict[str, object]:
        status = self.status.get(session.notification_id, SessionStatus())
        if not session.enabled:
            chip = badge("Paused", "off")
        elif status.running:
            chip = badge("Renewing...", "warn")
        elif status.maintenance:
            chip = badge("LG maintenance", "warn")
        elif status.tv_alert is not None:
            chip = badge(status.tv_state, "error")
        elif status.last_ok is None:
            chip = badge("Not run yet")
        elif status.needs_signin:
            chip = badge("Sign in on TV", "error")
        else:
            chip = badge("Renewed", "ok") if status.last_ok else badge("Failed", "error")
        stats = [
            stat("Last attempt", _clock(status.last_attempt)),
            stat("Last success", _clock(status.last_success)),
            stat("Next renewal", _clock(self.next_run) if session.enabled else "Paused"),
            stat("Time left", None if status.remaining_hours is None else int(status.remaining_hours), "h"),
        ]
        actions = [
            action(
                "renew", "Renew now", {"session": session.notification_id}, disabled=status.running, icon="mdi:refresh"
            )
        ]
        if session.host:
            stats.append(stat("TV token", status.tv_state or "Not checked"))
            stats.append(stat("Last TV check", _clock(status.tv_checked)))
            stats.append(stat("SSH key", self._key_text(session)))
            actions.append(
                action("check_tv", "Check TV now", {"session": session.notification_id}, icon="mdi:television")
            )
            actions.append(
                action(
                    "upload_key",
                    "Upload SSH key",
                    {"session": session.notification_id},
                    disabled=session.private_key is not None,
                    reason="The private_key option is used instead",
                    icon="mdi:upload",
                    upload="*/*",
                )
            )
            if status.tv_alert == "hostkey":
                actions.append(
                    action(
                        "trust_tv",
                        "Trust this TV again",
                        {"session": session.notification_id},
                        confirm="Only do this if you reset or replaced the TV. Forget its old SSH identity?",
                        danger=True,
                        icon="mdi:shield-alert",
                    )
                )
        return {
            "title": session.name,
            "icon": "mdi:television",
            "subtitle": "Custom renewal URL" if session.custom_url else "Developer Mode session token",
            "badge": chip,
            "off": not session.enabled,
            "stats": stats,
            "toggles": [
                toggle(
                    f"session:{session.notification_id}:enabled",
                    "Auto renew",
                    session.enabled,
                    "Renew this TV's Developer Mode session on the schedule",
                    icon="mdi:autorenew",
                )
            ],
            "actions": actions,
        }

    @staticmethod
    def _maintenance_text(config: Config) -> str:
        windows = list(config.maintenance_windows)
        text = maintenance.describe(windows)
        until = maintenance.window_end(windows, datetime.now().astimezone())
        return f"{text} (now, until {until:%H:%M})" if until else text

    def panel_state(self) -> dict[str, object]:
        """The panel view model."""

        config = self.view_config()
        phones = sorted(self.discovered)
        enabled = sum(s.enabled for s in config.sessions)
        return {
            "title": "LG webOS DevMode Mgmt",
            "subtitle": f"Version {VERSION} · {enabled} of {len(config.sessions)} TV(s) on auto renew",
            "sections": [
                {
                    "title": "TVs",
                    "icon": "mdi:television-play",
                    "cards": [self._session_card(s) for s in config.sessions],
                },
                {
                    "title": "Settings",
                    "icon": "mdi:cog",
                    "cards": [
                        {
                            "title": "Schedule",
                            "icon": "mdi:calendar-clock",
                            "subtitle": "Change the interval, retries, tokens and TVs in the Configuration tab",
                            "stats": [
                                stat("Renew every", config.interval_hours, "h"),
                                stat("Retries", config.retries),
                                stat("Next renewal", _clock(self.next_run)),
                                stat("Maintenance windows", self._maintenance_text(config)),
                            ],
                            "toggles": [
                                toggle(
                                    "notify_on_failure",
                                    "Failure notifications",
                                    config.notify_on_failure,
                                    "Notify the phones below when a renewal fails after all retries",
                                    icon="mdi:bell",
                                )
                            ],
                            "actions": [action("renew_all", "Renew all now", icon="mdi:refresh")],
                        },
                        {
                            "title": "Phones",
                            "icon": "mdi:cellphone-message",
                            "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
                            "toggles": [
                                toggle(
                                    f"phone:{name}",
                                    name.removeprefix(MOBILE_TARGET_PREFIX).replace("_", " "),
                                    self.phones.get(name, True),
                                    name,
                                    disabled=not config.notify_on_failure,
                                    icon="mdi:cellphone",
                                )
                                for name in phones
                            ],
                            "empty": "No mobile apps found. Without phones a persistent notification is used.",
                        },
                    ],
                },
            ],
        }


SETUP_HELP = (
    "Open Settings → Apps → LG webOS DevMode Mgmt → Configuration, fix the option named above and save. "
    "This page picks up the change within 15 seconds; no restart needed."
)
SETUP_EXAMPLE = """interval_hours: 48
retries: 3
notify_on_failure: true
sessions:
  - name: living-room-tv
    token: YOUR_DEV_MODE_SESSION_TOKEN"""


def _load_config(
    token: str, secrets: SecretRefs, router: PanelRouter, stop: threading.Event
) -> tuple[dict[str, object], Config] | None:
    """The stored options (!secret references kept) and their config; waits in the panel while invalid."""

    raw_options = initial_options(load_options, lambda: fetch_options(token), secrets)

    def validate(raw: dict[str, object]) -> Config:
        return load_config(secrets.resolve_options(raw))

    try:
        return raw_options, validate(raw_options)
    except (ConfigError, SecretError) as err:
        _LOGGER.error("Configuration problem: %s", err)  # noqa: TRY400 - no traceback needed
        _LOGGER.info("Waiting for a valid configuration; open the LG DevMode panel for details")
        view = SetupView("LG webOS DevMode Mgmt", str(err), SETUP_HELP, SETUP_EXAMPLE)
        router.target = view
        result = wait_for_valid_config(lambda: fetch_options(token), validate, view, stop)
        if result is None:
            return None
        raw, config = result
        if not isinstance(config, Config):
            raise TypeError("the options were validated into something that is not a Config") from None
        _LOGGER.info("Configuration is valid now; starting")
        return raw, config


def main() -> None:
    """Entry point."""

    _merge_s6_environment()  # TZ first, so every log line uses local time
    setup_logging()
    _LOGGER.info("Starting LG webOS DevMode Mgmt %s", VERSION)
    token = load_supervisor_token()

    stop = threading.Event()
    router = PanelRouter()

    def _stop(signum: int, _frame: object) -> None:
        _LOGGER.info("Received signal %d; shutting down", signum)
        stop.set()
        if isinstance(router.target, App):
            router.target.stopping.set()
            router.target.wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    server = PanelServer(WEB_DIR, router.get_state, router.on_toggle, router.on_action)
    server.start()
    try:
        secrets = SecretRefs()
        loaded = _load_config(token, secrets, router, stop)
        if loaded is None:
            return
        raw_options, config = loaded

        if config.log_level != "info":
            _LOGGER.info("Log level set to %s", config.log_level)
        apply_log_level(config.log_level)
        _LOGGER.info(
            "Configured %d session(s), interval=%s h, retries=%s, notify=%s",
            len(config.sessions),
            config.interval_hours,
            config.retries,
            config.notify_on_failure,
        )
        for session in config.sessions:
            _LOGGER.info("Session '%s'%s", session.name, "" if session.enabled else " (paused)")

        app = App(config, raw_options, token)
        app.secrets = secrets
        router.target = app
        if stop.is_set():
            return
        app.migrate_overrides()
        app.refresh_phones()
        app.next_phone_check = time.monotonic() + PHONE_CHECK_SECONDS
        targets = effective_mobile_targets(app.discovered, app.phones)
        _LOGGER.info("Notifications go to %d phone(s): %s", len(targets), ", ".join(targets) or "-")
        app.run()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
