"""Supervisor / Home Assistant Core API helpers (shared pattern with the other apps in this repo)."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import load_options

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_API = "http://supervisor"
SUPERVISOR_CORE_API = f"{SUPERVISOR_API}/core/api"
SUPERVISOR_TOKEN_ENV = "SUPERVISOR_TOKEN"  # noqa: S105 - env var name, not a secret
SUPERVISOR_TOKEN_ENV_CANDIDATES = (
    "SUPERVISOR_TOKEN",
    "SUPERVISOR_HASS_TOKEN",
    "HASSIO_TOKEN",
    "HOMEASSISTANT_TOKEN",
    "SUPERVISOR_API_TOKEN",
)
MOBILE_TARGET_PREFIX = "mobile_app_"
HTTP_TIMEOUT = 30
_CONTAINER_ENV_DIRS = (Path("/run/s6/container_environment"), Path("/run/s6-container-env"))


def merge_s6_environment() -> None:
    """Import s6 container environment vars into ``os.environ``.

    With ``init: false`` and no ``with-contenv`` wrapper the Supervisor-injected
    variables (``SUPERVISOR_TOKEN``, ``TZ``...) only live in the s6 env dir.
    Values carry a trailing newline that must be stripped for a valid header.
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
        time.tzset()  # pick up the merged TZ so log lines use local time


def load_supervisor_token() -> str:
    """Return the supervisor token from the container environment ("" when missing)."""

    merge_s6_environment()
    for name in SUPERVISOR_TOKEN_ENV_CANDIDATES:
        value = os.environ.get(name, "")
        if value:
            if name != SUPERVISOR_TOKEN_ENV:
                _LOGGER.debug("Using %s for the supervisor token", name)
            return value

    visible = ", ".join(sorted(k for k in os.environ if k.startswith(("SUPERVISOR_", "HASS", "HOMEASSISTANT"))))
    _LOGGER.warning(
        "No supervisor token found; scanned %s. Supervisor env vars present: %s",
        ", ".join(SUPERVISOR_TOKEN_ENV_CANDIDATES),
        visible or "(none)",
    )
    return ""


class SupervisorError(RuntimeError):
    """Raised when a Supervisor or Core API call fails."""


class Supervisor:
    """Thin JSON client for the Supervisor and the proxied Core REST API."""

    def __init__(self, token: str) -> None:
        self.token = token

    def _request(self, url: str, method: str = "GET", body: object | None = None) -> object:
        if not self.token:
            raise SupervisorError("No supervisor token available")
        request = urllib.request.Request(  # noqa: S310 - internal supervisor API
            url,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310 - internal supervisor API
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            raise SupervisorError(f"HTTP {err.code} from {method} {url.removeprefix(SUPERVISOR_API)}") from err
        except (urllib.error.URLError, OSError) as err:
            raise SupervisorError(f"{method} {url.removeprefix(SUPERVISOR_API)} failed: {err}") from err
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as err:
            raise SupervisorError(f"Non-JSON reply from {url.removeprefix(SUPERVISOR_API)}") from err

    def core_get(self, path: str) -> object:
        """GET from the Core REST API (``path`` starts with '/')."""

        return self._request(f"{SUPERVISOR_CORE_API}{path}")

    def core_post(self, path: str, body: object) -> object:
        """POST to the Core REST API."""

        return self._request(f"{SUPERVISOR_CORE_API}{path}", "POST", body)

    def core_delete(self, path: str) -> None:
        """DELETE on the Core REST API (a missing resource is fine)."""

        try:
            self._request(f"{SUPERVISOR_CORE_API}{path}", "DELETE")
        except SupervisorError as err:
            if "HTTP 404" not in str(err):
                raise

    def get_state(self, entity_id: str) -> dict[str, object] | None:
        """Return the state object of an entity, or None when it does not exist."""

        try:
            state = self.core_get(f"/states/{entity_id}")
        except SupervisorError as err:
            if "HTTP 404" in str(err):
                return None
            raise
        return state if isinstance(state, dict) else None

    def mqtt_service(self) -> dict[str, object] | None:
        """The MQTT broker the Supervisor offers (``services: mqtt:want``), or None when there is none.

        Supervisor answers HTTP 400 while no app (e.g. Mosquitto) provides MQTT.
        Other failures (Supervisor unreachable) raise, so callers keep their current broker.
        """

        try:
            reply = self._request(f"{SUPERVISOR_API}/services/mqtt")
        except SupervisorError as err:
            if "HTTP 400" in str(err) or "HTTP 404" in str(err):
                return None
            raise
        data = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(data, dict) or not data.get("host"):
            return None
        return data

    def fetch_options(self) -> dict[str, object]:
        """Current saved options from the Supervisor (includes edits made in the Configuration tab)."""

        reply = self._request(f"{SUPERVISOR_API}/addons/self/info")
        data = reply.get("data") if isinstance(reply, dict) else None
        options = data.get("options") if isinstance(data, dict) else None
        if not isinstance(options, dict):
            raise SupervisorError("Supervisor returned no app options")
        return options

    def write_options(self, options: dict[str, object]) -> None:
        """Replace the saved options (the payload must be wrapped as {"options": ...})."""

        self._request(f"{SUPERVISOR_API}/addons/self/options", "POST", {"options": options})

    def save_options(self, updates: dict[str, object]) -> None:
        """Merge ``updates`` into the saved options."""

        try:
            options = self.fetch_options()
        except SupervisorError:
            options = load_options()
        options.update(updates)
        self.write_options(options)

    def discover_mobile_targets(self) -> list[str] | None:
        """Every linked ``notify.mobile_app_*`` service name, or None when Home Assistant is unreachable."""

        try:
            services = self.core_get("/services")
        except SupervisorError:
            _LOGGER.warning("Could not list mobile-app notification targets; keeping the known phones")
            return None
        if not isinstance(services, list):
            return None
        names = {
            str(name)
            for entry in services
            if isinstance(entry, dict) and entry.get("domain") == "notify"
            for name in (entry.get("services") or {})
        }
        return sorted(name for name in names if name.startswith(MOBILE_TARGET_PREFIX))

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:
        """Push to the given mobile apps, or create a persistent notification when there are none."""

        if targets:
            for target in targets:
                try:
                    self.core_post(f"/services/notify/{target}", {"title": title, "message": message})
                    _LOGGER.info("Notification sent to '%s'", target)
                except SupervisorError:
                    _LOGGER.exception("Failed to send notification to '%s'", target)
            return
        try:
            self.core_post(
                "/services/persistent_notification/create",
                {"notification_id": notification_id, "title": title, "message": message},
            )
        except SupervisorError:
            _LOGGER.exception("Failed to create persistent notification")


def effective_targets(discovered: list[str], phones: dict[str, object]) -> list[str]:
    """Linked phones that are not switched off (new phones default to on)."""

    return [t for t in discovered if phones.get(t, True) is not False]


def merge_discovered(phones: dict[str, object], discovered: list[str]) -> dict[str, bool]:
    """Switches for exactly the linked phones: new phones on, unlinked phones dropped."""

    return {name: phones.get(name, True) is not False for name in discovered}


def overrides_from_options(raw_options: dict[str, object]) -> dict[str, bool]:
    """Phone switches from the retired ``notification_overrides`` option (migration)."""

    rows = raw_options.get("notification_overrides")
    prefs: dict[str, bool] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str(row.get("name") or "").strip():
            prefs[str(row["name"]).strip()] = bool(row.get("notify", True))
    return prefs


def notification_id(account_id: str) -> str:
    """Persistent-notification id for an account."""

    return "cloudflare_tunnel_" + re.sub(r"[^a-z0-9_]+", "_", account_id)
