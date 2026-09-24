#!/usr/bin/env python3
"""Home Assistant add-on: LG webOS Developer Mode session auto renew."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path("/data/options.json")
LG_DEV_ENDPOINT = "https://developer.lge.com/secure/ResetDevModeSession.dev"
SUPERVISOR_CORE_API = "http://supervisor/core/api"

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


class ConfigError(ValueError):
    """Raised when the add-on configuration is invalid."""


@dataclass(slots=True)
class Session:
    """A single LG webOS device whose dev session is renewed."""

    name: str
    url: str
    notification_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.notification_id = re.sub(r"[^a-z0-9_-]+", "-", self.name.lower()).strip("-")


@dataclass(slots=True)
class Config:
    """Parsed add-on options."""

    interval_hours: int
    retries: int
    notify_on_failure: bool
    sessions: list[Session]


def setup_logging() -> None:
    """Configure logging to stdout so the supervisor captures it."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


MAX_PROBE_FILE_SIZE = 2048
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


def _log_container_env_probe() -> None:
    """Log what the container environment actually contains (diagnostic)."""

    env_keys = sorted(os.environ)
    _LOGGER.info("Container env var names (%d): %s", len(env_keys), ", ".join(env_keys[:80]))

    def _list_dir(path: str) -> None:
        if not os.path.isdir(path):
            return
        entries = sorted(os.listdir(path))
        _LOGGER.info("%s entries (%d): %s", path, len(entries), ", ".join(entries[:60]))
        for name in entries:
            entry = os.path.join(path, name)
            if os.path.isfile(entry) and os.path.getsize(entry) <= MAX_PROBE_FILE_SIZE:
                try:
                    content = Path(entry).read_text(encoding="utf-8", errors="replace").strip()
                    _LOGGER.info("  %s = %s", name, content[:400])
                except OSError:
                    pass

    try:
        _list_dir("/run")
        _list_dir("/run/supervisor")
        for directory in _CONTAINER_ENV_DIRS:
            if directory.is_dir():
                names = ", ".join(sorted(p.name for p in directory.iterdir()))
                _LOGGER.info("%s env var files: %s", directory, names or "(empty)")
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
            return value

    visible = ", ".join(
        sorted(key for key in os.environ if key.startswith(("SUPERVISOR_", "HASS", "HOMEASSISTANT")))
    )
    _LOGGER.warning(
        "No supervisor token found; scanned %s. Supervisor env vars present: %s",
        ", ".join(SUPERVISOR_TOKEN_ENV_CANDIDATES),
        visible or "(none)",
    )
    _log_container_env_probe()
    return ""


def mask_token(url: str) -> str:
    """Return the URL with the sessionToken query parameter masked."""

    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    masked = [(key, "***" if key.lower() == "sessiontoken" else value) for key, value in query]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(masked), parts.fragment)
    )


def build_sessions(raw_sessions: list[object]) -> list[Session]:
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
            if not url.startswith(("http://", "https://")):
                raise ConfigError(f"Session '{name}': url must start with http:// or https://")
        elif token:
            url = f"{LG_DEV_ENDPOINT}?sessionToken={urllib.parse.quote(token, safe='')}"
        else:
            raise ConfigError(f"Session '{name}' requires either a token or a full url")

        sessions.append(Session(name=name, url=url))

    return sessions


MOBILE_TARGET_PREFIX = "mobile_app_"


def load_config(raw_config: dict[str, object]) -> Config:
    """Validate raw options and produce a Config."""

    interval_raw = raw_config.get("interval_hours", 48)
    retries_raw = raw_config.get("retries", 3)

    interval_hours = interval_raw if isinstance(interval_raw, int) else 48
    retries = retries_raw if isinstance(retries_raw, int) else 3

    sessions_raw = raw_config.get("sessions")
    sessions = build_sessions(sessions_raw if isinstance(sessions_raw, list) else [])

    return Config(
        interval_hours=interval_hours,
        retries=retries,
        notify_on_failure=bool(raw_config.get("notify_on_failure", True)),
        sessions=sessions,
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


def handle_response(body: str, status: int, session_name: str, masked_url: str) -> bool:
    """Interpret the LG Dev API response; returns whether the renewal succeeded."""

    if not body.strip():
        _LOGGER.info("Renewal for '%s' returned an empty body (HTTP %s); assuming success", session_name, status)
        return True

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        _LOGGER.info("Renewal for '%s' returned non-JSON (HTTP %s); assuming success", session_name, status)
        return True

    if not isinstance(payload, dict):
        _LOGGER.info(
            "Renewal for '%s' returned an unexpected payload shape (HTTP %s); assuming success", session_name, status
        )
        return True

    result = str(payload.get("result") or "").lower()
    error_code = str(payload.get("errorCode") or "")
    error_msg = str(payload.get("errorMsg") or "")

    if result in SUCCESS_RESULTS and error_code == "200":
        if error_msg and error_msg not in INFO_MSG_CODES:
            _LOGGER.info("Renewal for '%s' succeeded: %s", session_name, error_msg)
        else:
            _LOGGER.info("Renewal for '%s' succeeded", session_name)
        return True

    _LOGGER.error(
        "Renewal for '%s' failed per LG API: result=%s errorCode=%s errorMsg=%s (%s)",
        session_name,
        result,
        error_code,
        error_msg,
        masked_url,
    )
    return False


def renew_session(session: Session) -> bool:
    """Renew one dev session and return whether it succeeded."""

    masked_url = mask_token(session.url)
    _LOGGER.info("Renewing dev session '%s' (%s)", session.name, masked_url)
    try:
        with urllib.request.urlopen(session.url, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - admin-supplied LG URL
            body = response.read().decode("utf-8", errors="replace")
            return handle_response(body, response.status, session.name, masked_url)
    except urllib.error.HTTPError as err:
        _LOGGER.exception("Renewal for '%s' failed: HTTP %s %s (%s)", session.name, err.code, err.reason, masked_url)
        return False
    except urllib.error.URLError as err:
        _LOGGER.exception("Renewal for '%s' failed: %s (%s)", session.name, err.reason, masked_url)
        return False
    except OSError:
        _LOGGER.exception("Renewal for '%s' failed: %s", session.name, masked_url)
        return False


def renew_with_retries(session: Session, retries: int, backoff_seconds: int = 30) -> bool:
    """Renew a session, retrying with exponential backoff on failure."""

    for attempt in range(retries + 1):
        if renew_session(session):
            return True
        if attempt < retries:
            wait = backoff_seconds * (2**attempt)
            _LOGGER.warning(
                "Renewal for '%s' failed; retrying in %s s (attempt %d/%d)",
                session.name,
                wait,
                attempt + 1,
                retries,
            )
            time.sleep(wait)
    return False


def discover_mobile_targets(supervisor_token: str) -> list[str]:
    """Discover notify.mobile_app_* targets from existing Home Assistant services.

    Every Companion app on the internal Home Assistant API registers a
    ``notify.mobile_app_<device>`` service; each such service becomes a target.
    Returns [] (falling back to a persistent notification) when nothing is
    found or the API is unreachable.
    """

    if not supervisor_token:
        return []

    def _json_get(path: str) -> object:
        request = urllib.request.Request(  # noqa: S310 - internal supervisor API
            f"{SUPERVISOR_CORE_API}{path}",
            method="GET",
            headers={"Authorization": f"Bearer {supervisor_token}"},
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - internal supervisor API
            return json.loads(response.read().decode("utf-8"))

    try:
        services = _json_get("/services")
    except (urllib.error.URLError, OSError, ValueError):
        _LOGGER.exception("Failed to discover mobile-app notification targets")
        return []

    if not isinstance(services, list):
        _LOGGER.warning("Unexpected /services response (expected a list): %s", str(services)[:200])
        return []

    notify_targets = {
        str(name)
        for entry in services
        if isinstance(entry, dict) and entry.get("domain") == "notify"
        for name in (entry.get("services") or {})
    }
    return sorted(t for t in notify_targets if t.startswith(MOBILE_TARGET_PREFIX))


def notify_failure(session: Session, targets: list[str], supervisor_token: str) -> None:
    """Notify about a failed renewal to Home Assistant.

    Routes to the HA notify service (e.g. the mobile app) when notification
    targets are configured; otherwise falls back to a persistent notification.
    """

    if not supervisor_token:
        _LOGGER.warning("Cannot notify for '%s': no supervisor token available", session.name)
        return

    title = "LG webOS Dev Session Renewal Failed"
    message = (
        f"The LG webOS Developer Mode session '{session.name}' could not be renewed "
        "after exhausting all retries. Renew it manually or the dev session may expire."
    )

    if targets:
        for target in targets:
            request = urllib.request.Request(  # noqa: S310 - internal supervisor API
                f"{SUPERVISOR_CORE_API}/services/notify/{target}",
                data=json.dumps({"title": title, "message": message}).encode("utf-8"),
                method="POST",
                headers={
                    "Authorization": f"Bearer {supervisor_token}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT):  # noqa: S310 - internal supervisor API
                    _LOGGER.info("Failure notification sent to '%s' for '%s'", target, session.name)
            except (urllib.error.URLError, OSError):
                _LOGGER.exception("Failed to send notification to '%s' for '%s'", target, session.name)
        return

    request = urllib.request.Request(  # noqa: S310 - internal supervisor API
        f"{SUPERVISOR_CORE_API}/services/persistent_notification/create",
        data=json.dumps(
            {
                "notification_id": session.notification_id,
                "title": title,
                "message": message,
            }
        ).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {supervisor_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT):  # noqa: S310 - internal supervisor API
            _LOGGER.info("Failure notification sent for '%s'", session.name)
    except (urllib.error.URLError, OSError):
        _LOGGER.exception("Failed to send notification for '%s'", session.name)


def renew_all(config: Config, mobile_targets: list[str], supervisor_token: str) -> None:
    """Renew every session and notify about any that could not be renewed."""

    failures = [session for session in config.sessions if not renew_with_retries(session, config.retries)]
    if failures and config.notify_on_failure:
        for session in failures:
            notify_failure(session, mobile_targets, supervisor_token)


def run(config: Config, supervisor_token: str) -> None:
    """Run the initial renewal and then the scheduling loop."""

    interval_seconds = config.interval_hours * 3600
    mobile_targets = discover_mobile_targets(supervisor_token)
    _LOGGER.info(
        "Auto-discovered %d mobile notification target(s): %s",
        len(mobile_targets),
        ", ".join(mobile_targets) or "-",
    )
    _LOGGER.info("Performing initial dev session renewal")
    renew_all(config, mobile_targets, supervisor_token)

    next_run = time.monotonic() + interval_seconds
    _LOGGER.info(
        "Next renewal scheduled in %s h for %s",
        config.interval_hours,
        time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(time.time() + interval_seconds)),
    )

    while True:
        remaining = next_run - time.monotonic()
        if remaining > 0:
            time.sleep(min(60.0, remaining))
            continue

        _LOGGER.info("Scheduled dev session renewal started")
        renew_all(config, mobile_targets, supervisor_token)
        next_run = time.monotonic() + interval_seconds
        _LOGGER.info(
            "Renewal completed; next run in %s h for %s",
            config.interval_hours,
            time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(time.time() + interval_seconds)),
        )


def main() -> None:
    """Entry point."""

    setup_logging()
    _LOGGER.info("Starting LG webOS Dev Session Auto Renew")

    try:
        config = load_config(load_options())
    except ConfigError:
        _LOGGER.exception("Invalid configuration")
        raise SystemExit(1) from None

    _LOGGER.info(
        "Configured %d session(s), interval=%s h, retries=%s, notify=%s",
        len(config.sessions),
        config.interval_hours,
        config.retries,
        config.notify_on_failure,
    )
    for session in config.sessions:
        _LOGGER.info("Session '%s' -> %s", session.name, mask_token(session.url))

    run(config, load_supervisor_token())


if __name__ == "__main__":
    main()
