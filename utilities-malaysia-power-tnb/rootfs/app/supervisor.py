"""Supervisor / Home Assistant Core API helpers (ported from lg-webos-auto-renew)."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from config import ConfigError, NotificationOverride, load_options

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


def _merge_s6_environment() -> None:
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


def load_supervisor_token() -> str:
    """Return the supervisor token from the container environment ("" when missing)."""

    _merge_s6_environment()
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

    def get_state(self, entity_id: str) -> dict[str, object] | None:
        """Return the state object of an entity, or None when it does not exist."""

        try:
            state = self.core_get(f"/states/{entity_id}")
        except SupervisorError as err:
            if "HTTP 404" in str(err):
                return None
            raise
        return state if isinstance(state, dict) else None

    def mqtt_service(self) -> dict[str, object]:
        """Return the MQTT broker credentials granted via ``services: mqtt:need``."""

        reply = self._request(f"{SUPERVISOR_API}/services/mqtt")
        data = reply.get("data") if isinstance(reply, dict) else None
        if not isinstance(data, dict) or not data.get("host"):
            raise SupervisorError("No MQTT service available; install and start the Mosquitto broker add-on")
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

    def discover_mobile_targets(self) -> list[str]:
        """Return every ``notify.mobile_app_*`` service name ([] when none or unreachable)."""

        try:
            services = self.core_get("/services")
        except SupervisorError:
            _LOGGER.exception("Failed to discover mobile-app notification targets")
            return []
        if not isinstance(services, list):
            return []
        names = {
            str(name)
            for entry in services
            if isinstance(entry, dict) and entry.get("domain") == "notify"
            for name in (entry.get("services") or {})
        }
        return sorted(name for name in names if name.startswith(MOBILE_TARGET_PREFIX))

    def seed_notification_overrides(
        self, overrides: list[NotificationOverride], discovered: list[str]
    ) -> list[NotificationOverride]:
        """Add newly discovered phones (notify on) and persist the list so toggles show in the UI."""

        merged = list(overrides)
        known = {o.name for o in merged}
        added = [NotificationOverride(name=name) for name in discovered if name not in known]
        merged.extend(added)
        if added:
            try:
                self.save_options({"notification_overrides": [{"name": o.name, "notify": o.notify} for o in merged]})
                _LOGGER.info("Added %d phone(s) to notification_overrides", len(added))
            except (SupervisorError, ConfigError):
                _LOGGER.warning("Could not persist notification overrides; toggles still apply for this run")
        return merged

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


def effective_targets(discovered: list[str], overrides: list[NotificationOverride]) -> list[str]:
    """Discovered phones minus the ones switched off."""

    off = {o.name for o in overrides if not o.notify}
    return [t for t in discovered if t not in off]


def notification_id(account_id: str) -> str:
    """Persistent-notification id for an account."""

    return "power_tnb_" + re.sub(r"[^a-z0-9_]+", "_", account_id)
