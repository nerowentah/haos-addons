#!/usr/bin/env python3
"""Home Assistant add-on: LG webOS Developer Mode session auto renew."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import maintenance
from webui import (
    PanelError,
    PanelRouter,
    PanelServer,
    SetupView,
    action,
    badge,
    stat,
    toggle,
    wait_for_valid_config,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = Path("/data/options.json")
PHONES_PATH = Path("/data/phones.json")
WEB_DIR = Path(__file__).resolve().parent / "web"
LG_DEV_ENDPOINT = "https://developer.lge.com/secure/ResetDevModeSession.dev"
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
LOOP_SECONDS = 30
OPTIONS_CHECK_SECONDS = 30
PHONE_CHECK_SECONDS = 300


class ConfigError(ValueError):
    """Raised when the add-on configuration is invalid."""


def slugify(value: str) -> str:
    """Lower-case id used for notifications and panel toggles."""

    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")


@dataclass(slots=True)
class Session:
    """A single LG webOS device whose dev session is renewed."""

    name: str
    url: str
    enabled: bool = True
    custom_url: bool = False
    notification_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.notification_id = slugify(self.name)


@dataclass(slots=True)
class Config:
    """Parsed add-on options."""

    interval_hours: int
    retries: int
    notify_on_failure: bool
    sessions: list[Session]
    maintenance_windows: list[maintenance.Window] = field(default_factory=list)


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
    if hasattr(time, "tzset"):
        time.tzset()  # pick up the merged TZ so panel times are local


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

    visible = ", ".join(sorted(key for key in os.environ if key.startswith(("SUPERVISOR_", "HASS", "HOMEASSISTANT"))))
    _LOGGER.warning(
        "No supervisor token found; scanned %s. Supervisor env vars present: %s",
        ", ".join(SUPERVISOR_TOKEN_ENV_CANDIDATES),
        visible or "(none)",
    )
    _log_container_env_probe()
    return ""


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

        enabled = entry.get("enabled")
        sessions.append(
            Session(
                name=name,
                url=url,
                enabled=enabled if isinstance(enabled, bool) else True,
                custom_url=bool(entry.get("url")),
            )
        )

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

    try:
        windows = maintenance.parse_windows(raw_config.get("maintenance_windows"))
    except maintenance.MaintenanceConfigError as err:
        raise ConfigError(f"maintenance_windows: {err}") from err

    return Config(
        interval_hours=interval_hours,
        retries=retries,
        notify_on_failure=bool(raw_config.get("notify_on_failure", True)),
        sessions=sessions,
        maintenance_windows=windows,
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


def handle_response(body: str, status: int, session_name: str) -> bool:
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
        "Renewal for '%s' failed per LG API: result=%s errorCode=%s errorMsg=%s",
        session_name,
        result,
        error_code,
        error_msg,
    )
    return False


class VendorMaintenanceError(Exception):
    """LG's developer site answered with a maintenance page or HTTP 503."""


def renew_session(session: Session) -> bool:
    """Renew one dev session and return whether it succeeded (raises VendorMaintenanceError)."""

    _LOGGER.info("Renewing dev session '%s'", session.name)
    try:
        with urllib.request.urlopen(session.url, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - admin-supplied LG URL
            body = response.read().decode("utf-8", errors="replace")
            if maintenance.looks_like_maintenance(response.status, body):
                raise VendorMaintenanceError(f"LG developer site is under maintenance (HTTP {response.status})")
            return handle_response(body, response.status, session.name)
    except urllib.error.HTTPError as err:
        if maintenance.looks_like_maintenance(err.code):
            raise VendorMaintenanceError(f"LG developer site is under maintenance (HTTP {err.code})") from err
        _LOGGER.exception("Renewal for '%s' failed: HTTP %s %s", session.name, err.code, err.reason)
        return False
    except urllib.error.URLError as err:
        _LOGGER.exception("Renewal for '%s' failed: %s", session.name, err.reason)
        return False
    except OSError:
        _LOGGER.exception("Renewal for '%s' failed", session.name)
        return False


def renew_with_retries(
    session: Session,
    retries: int,
    backoff_seconds: int = 30,
    sleep: Callable[[float], object] = time.sleep,
) -> bool:
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
            sleep(wait)
    return False


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
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".phones-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(prefs, handle, sort_keys=True)
        os.replace(tmp, path)
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


def notify_failure(session: Session, targets: list[str], supervisor_token: str) -> None:
    """Notify about a failed renewal to Home Assistant.

    Routes to the HA notify service (e.g. the mobile app) when notification
    targets are configured; otherwise falls back to a persistent notification.
    """

    if not supervisor_token:
        _LOGGER.warning("Cannot notify for '%s': no supervisor token available", session.name)
        return

    title = "LG webOS DevMode renewal failed"
    message = (
        f"The LG webOS Developer Mode session '{session.name}' could not be renewed "
        "after exhausting all retries. Renew it manually or the dev session may expire."
    )

    if targets:
        for target in targets:
            try:
                _supervisor_request(
                    supervisor_token, f"/core/api/services/notify/{target}", {"title": title, "message": message}
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
            for row in options.get("sessions") or []:  # type: ignore[union-attr]
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
    ) -> None:
        self.config = config
        self.raw_options = raw_options
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

    # -- options ------------------------------------------------------------
    def view_config(self) -> Config:
        """Config the panel should show (a saved-but-not-yet-applied change wins)."""

        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        """Switch to new options in place (main thread)."""

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
            except (urllib.error.URLError, OSError, ValueError, ConfigError):
                base = self.raw_options
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(raw)
            except ConfigError as err:
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
            config = load_config(raw)
        except ConfigError as err:
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
            stored = self.raw_options
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
    def renew(self, sessions: list[Session]) -> bool:
        """Renew the given sessions, recording status and notifying on failure.

        Returns False when LG's site answered with a maintenance page: the
        remaining sessions are skipped and no failure is reported.
        """

        for session in sessions:
            if self.stopping.is_set():
                return True
            status = self.status.setdefault(session.notification_id, SessionStatus())
            status.running = True
            status.last_attempt = time.time()
            try:
                ok = renew_with_retries(session, self.config.retries, sleep=self.stopping.wait)
            except VendorMaintenanceError as err:
                status.running = False
                for other in sessions:
                    self.status.setdefault(other.notification_id, SessionStatus()).maintenance = True
                self._retry_after_maintenance(str(err))
                return False
            status.running = False
            status.maintenance = False
            status.last_ok = ok
            if ok:
                status.last_success = time.time()
            elif self.config.notify_on_failure:
                targets = effective_mobile_targets(self.discovered, self.phones)
                notify_failure(session, targets, self.token)
        self.backoff.reset()
        return True

    def maintenance_until(self) -> datetime | None:
        """End of the current LG maintenance window (Home Assistant's timezone), or None."""

        return maintenance.window_end(self.config.maintenance_windows, datetime.now().astimezone())

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
                    maintenance.describe(self.config.maintenance_windows),
                    time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(self.next_run)),
                )
                return
            enabled = [s for s in self.config.sessions if s.enabled]
            _LOGGER.info("Scheduled renewal of %d TV(s)", len(enabled))
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

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Queue a renewal from a panel button."""

        if action_id == "renew_all":
            target: str | None = None
            label = "all TVs"
        elif action_id == "renew":
            target = str(args.get("session") or "")
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
        elif status.last_ok is None:
            chip = badge("Not run yet")
        else:
            chip = badge("Renewed", "ok") if status.last_ok else badge("Failed", "error")
        return {
            "title": session.name,
            "icon": "mdi:television",
            "subtitle": "Custom renewal URL" if session.custom_url else "Developer Mode session token",
            "badge": chip,
            "off": not session.enabled,
            "stats": [
                stat("Last attempt", _clock(status.last_attempt)),
                stat("Last success", _clock(status.last_success)),
                stat("Next renewal", _clock(self.next_run) if session.enabled else "Paused"),
            ],
            "toggles": [
                toggle(
                    f"session:{session.notification_id}:enabled",
                    "Auto renew",
                    session.enabled,
                    "Renew this TV's Developer Mode session on the schedule",
                    icon="mdi:autorenew",
                )
            ],
            "actions": [
                action(
                    "renew",
                    "Renew now",
                    {"session": session.notification_id},
                    disabled=status.running,
                    icon="mdi:refresh",
                ),
            ],
        }

    @staticmethod
    def _maintenance_text(config: Config) -> str:
        text = maintenance.describe(config.maintenance_windows)
        until = maintenance.window_end(config.maintenance_windows, datetime.now().astimezone())
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
        try:
            raw_options = load_options()
            config = load_config(raw_options)
        except ConfigError as err:
            _LOGGER.error("Configuration problem: %s", err)  # noqa: TRY400 - no traceback needed
            _LOGGER.info("Waiting for a valid configuration; open the LG DevMode panel for details")
            view = SetupView("LG webOS DevMode Mgmt", str(err), SETUP_HELP, SETUP_EXAMPLE)
            router.target = view
            result = wait_for_valid_config(lambda: fetch_options(token), load_config, view, stop)
            if result is None:
                return
            raw_options, config = result  # type: ignore[assignment]
            _LOGGER.info("Configuration is valid now; starting")

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
