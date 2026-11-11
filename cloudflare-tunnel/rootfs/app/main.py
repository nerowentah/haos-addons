#!/usr/bin/env python3
"""Home Assistant app: Cloudflare Tunnel - runs the cloudflared connector for a remotely-managed tunnel."""

from __future__ import annotations

import copy
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import cloudflared as cf
from config import Config, ConfigError, apply_log_level, load_config, load_options
from store import Store
from supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
)
from webui import (
    PanelError,
    PanelRouter,
    PanelServer,
    SecretError,
    SecretRefs,
    SetupView,
    action,
    badge,
    initial_options,
    notice,
    row,
    stat,
    toggle,
    wait_for_valid_config,
)

_LOGGER = logging.getLogger("cloudflare_tunnel")

VERSION = os.environ.get("CF_TUNNEL_VERSION", "dev")
LOOP_SECONDS = 5  # connector status poll cadence; the panel refreshes itself every 5s too
MIN_SLEEP_SECONDS = 1
OPTIONS_CHECK_SECONDS = 30
PHONE_CHECK_SECONDS = 300
STORE_SAVE_SECONDS = 60
FAILURES_BEFORE_NOTIFY = 6  # polls: ~30s of not being connected before the phones are told
WEB_DIR = Path(__file__).resolve().parent / "web"
NOTIFICATION_ID = "cloudflare_tunnel"

STATE_BADGE = {
    cf.STATE_STARTING: ("Starting", "neutral"),
    cf.STATE_CONNECTED: ("Connected", "ok"),
    cf.STATE_DEGRADED: ("Degraded", "warn"),
    cf.STATE_STOPPED: ("Restarting", "warn"),
    cf.STATE_ERROR: ("Error", "error"),
}
STATE_ICON = {
    cf.STATE_STARTING: "mdi:cloud-outline",
    cf.STATE_CONNECTED: "mdi:cloud-check-outline",
    cf.STATE_DEGRADED: "mdi:cloud-outline",
    cf.STATE_STOPPED: "mdi:cloud-off-outline",
    cf.STATE_ERROR: "mdi:cloud-alert",
}


def _ago(moment: datetime | None, now: datetime) -> str | None:
    """A short "x ago" label for the panel."""

    if moment is None:
        return None
    seconds = max(0, int((now - moment).total_seconds()))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


class App:
    """Runs the cloudflared connector, serves the panel and notifies when the tunnel fails."""

    def __init__(
        self,
        config: Config,
        supervisor: Supervisor,
        store: Store,
        raw_options: dict[str, object] | None = None,
        daemon: cf.Cloudflared | None = None,
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}  # as stored: !secret references, never their values
        self.secrets = SecretRefs()
        self.supervisor = supervisor
        self.store = store
        self.daemon = daemon or cf.Cloudflared(config)
        self.version = VERSION
        self.discovered: list[str] = []
        self.down_polls = 0
        self.down_notified = False
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.next_phone_check = 0.0
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None
        self.restart_requested = False

    # -- Options (panel + Configuration tab, applied without a restart) -------------
    def view_config(self) -> Config:
        """Config the panel should show (a saved-but-not-yet-applied change wins)."""

        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        """Switch to new options in place (main thread); restarts the connector when they matter."""

        old = self.config
        self.config = config
        self.raw_options = raw
        self.daemon.config = config
        if old.log_level != config.log_level:
            _LOGGER.info("Log level set to %s", config.log_level)  # before applying: may hide info
            apply_log_level(config.log_level)
        if old.daemon_spec() != config.daemon_spec():
            _LOGGER.info("Tunnel options changed; restarting the connector")
            self.daemon.restart()
        _LOGGER.info("Applied updated options")

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel; applied on the next loop pass (any thread)."""

        with self.options_lock:
            try:
                base = self.supervisor.fetch_options()
            except SupervisorError as err:
                # Never fall back to the start-up copy: it holds resolved !secret values.
                raise PanelError("Home Assistant is not reachable; try again") from err
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
            except (ConfigError, SecretError) as err:
                raise PanelError(str(err)) from err
            try:
                self.supervisor.write_options(raw)
            except SupervisorError as err:
                raise PanelError("Home Assistant rejected the change; see the app log") from err
            self.pending = (raw, config)
        self.wake.set()

    def check_options(self) -> None:
        """Pick up edits made in the Configuration tab."""

        try:
            raw = self.supervisor.fetch_options()
        except SupervisorError:
            return
        if raw == self.raw_options:
            return
        try:
            config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
        except (ConfigError, SecretError) as err:
            _LOGGER.error("Ignoring updated options until they are fixed: %s", err)  # noqa: TRY400 - no traceback needed
            self.raw_options = raw
            return
        self.apply_config(raw, config)

    # -- Phones (notification targets for this app) ---------------------------------
    @property
    def phones(self) -> dict[str, object]:
        """Per-phone switches kept in /data (phone name -> notify)."""

        return self.store.section("phones")

    def refresh_phones(self) -> None:
        """Re-list the phones linked to Home Assistant (new phones are notified by default)."""

        found = self.supervisor.discover_mobile_targets()
        if found is None:
            return
        with self.options_lock:
            merged = merge_discovered(self.phones, found)
            changed = merged != self.phones or found != self.discovered
            self.discovered = found
            if merged != self.phones:
                self.store.data["phones"] = merged
                self.store.dirty = True
        if changed:
            targets = effective_targets(self.discovered, self.phones)
            _LOGGER.info("Notifications go to %d phone(s): %s", len(targets), ", ".join(targets) or "-")

    def set_phone(self, name: str, value: bool) -> None:
        """Panel switch for one phone (kept in /data, applies immediately)."""

        with self.options_lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            self.store.dirty = True

    def notify(self, title: str, message: str, suffix: str = "") -> None:
        """Notify the switched-on phones (or a persistent notification) when enabled."""

        if self.config.notify_on_failure:
            targets = effective_targets(self.discovered, self.phones)
            self.supervisor.notify(targets, title, message, f"{NOTIFICATION_ID}{suffix}")

    # -- Failure notifications ------------------------------------------------------
    def watch(self) -> None:
        """Tell the phones once per outage when the tunnel is not connected (and when it recovers)."""

        if self.daemon.status.state == cf.STATE_CONNECTED:
            if self.down_notified:
                self.down_notified = False
                self.down_polls = 0
                self.notify("Cloudflare Tunnel recovered", "The tunnel is connected again.", "_down")
            else:
                self.down_polls = 0
            return
        self.down_polls += 1
        if self.down_polls >= FAILURES_BEFORE_NOTIFY and not self.down_notified:
            self.down_notified = True
            message = self.daemon.status.last_error or "The connector is not connected to Cloudflare's edge."
            self.notify("Cloudflare Tunnel is down", message, "_down")

    # -- Panel callbacks (HTTP thread) ---------------------------------------------------
    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        """Persist and apply one panel switch."""

        kind, _, name = toggle_id.partition(":")
        if kind == "phone" and name:
            self.set_phone(name, value)
            return
        if toggle_id == "notify_on_failure":
            self.update_options(lambda raw: raw.__setitem__("notify_on_failure", value))
            return
        raise PanelError("Unknown setting")

    def panel_action(self, action_id: str, _args: dict[str, object]) -> str | None:
        """Handle a panel button."""

        if action_id == "restart":
            self.restart_requested = True  # the loop restarts the connector on its own thread
            self.wake.set()
            return "Restarting the connector..."
        raise PanelError("Unknown action")

    # -- Panel view model ------------------------------------------------------------------
    def panel_state(self) -> dict[str, object]:
        """The panel view model."""

        config = self.view_config()
        status = self.daemon.status
        now = datetime.now(UTC)
        state = status.state
        text, tone = STATE_BADGE.get(state, ("Unknown", "warn"))
        tunnel: dict[str, object] = {
            "title": "Connector",
            "icon": STATE_ICON.get(state, "mdi:cloud-outline"),
            "subtitle": "Links this Home Assistant to Cloudflare's edge (managed from Zero Trust)",
            "badge": badge(text, tone),
            "stats": [
                stat("Edge connections", status.connections),
                stat("Restarts", status.restarts),
                stat("Started", _ago(status.started_at, now) or "-"),
                stat("Last change", _ago(status.last_change, now)),
            ],
            "actions": [
                action("restart", "Restart connector", confirm="Restart the tunnel connector now?", icon="mdi:restart"),
            ],
        }
        if status.last_error or state in (cf.STATE_ERROR, cf.STATE_DEGRADED):
            tunnel["notice"] = notice(
                status.last_error or "The connector is running but has no edge connection.",
                "error" if state == cf.STATE_ERROR else "warn",
            )
        settings: dict[str, object] = {
            "title": "Settings",
            "icon": "mdi:cog",
            "subtitle": "The tunnel token and advanced flags live in the Configuration tab",
            "stats": [stat("Log level", config.log_level), stat("Run parameters", len(config.run_parameters))],
            "toggles": [
                toggle(
                    "notify_on_failure",
                    "Notifications",
                    config.notify_on_failure,
                    "Notify the phones below when the tunnel has been down for about 30 seconds, and when it recovers",
                    icon="mdi:bell",
                ),
            ],
        }
        if config.run_parameters:
            settings["links"] = [row(item, "Run parameter") for item in config.run_parameters]
        phones = {
            "title": "Phones",
            "icon": "mdi:cellphone",
            "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
            "toggles": [
                toggle(
                    f"phone:{name}",
                    name.removeprefix("mobile_app_").replace("_", " "),
                    self.phones.get(name, True) is not False,
                    name,
                    disabled=not config.notify_on_failure,
                    icon="mdi:cellphone",
                )
                for name in sorted(self.discovered)
            ],
            "empty": "No mobile apps found. Without phones a persistent notification is used.",
        }
        sections: list[dict[str, object]] = [
            {"title": "Tunnel", "icon": "mdi:cloud-lock", "cards": [tunnel]},
            {"title": "Settings", "icon": "mdi:cog", "cards": [settings, phones]},
        ]
        return {
            "title": "Cloudflare Tunnel",
            "subtitle": f"Version {self.version} · cloudflared {status.version or 'starting'}",
            "sections": sections,
        }

    # -- Loop ----------------------------------------------------------------------------------
    def _tick(self) -> None:
        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        if self.restart_requested:
            self.restart_requested = False
            self.daemon.restart()
        now = time.monotonic()
        if now >= self.next_options_check:
            self.next_options_check = now + OPTIONS_CHECK_SECONDS
            self.check_options()
        if now >= self.next_phone_check:
            self.next_phone_check = now + PHONE_CHECK_SECONDS
            self.refresh_phones()
        self.daemon.poll()
        self.watch()

    def run(self) -> None:
        """Start the connector and run the loop until stopped."""

        self.daemon.start()
        last_save = time.monotonic()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                self._tick()
                if time.monotonic() - last_save >= STORE_SAVE_SECONDS or self.store.dirty:
                    self.store.save()
                    last_save = time.monotonic()
                deadline = min(self.next_options_check, self.next_phone_check)
                self.wake.wait(min(max(deadline - time.monotonic(), MIN_SLEEP_SECONDS), LOOP_SECONDS))
        finally:
            self.daemon.stop()
            self.store.save()
            _LOGGER.info("Stopped")


SETUP_HELP = (
    "Open Settings → Apps → Cloudflare Tunnel → Configuration, paste the tunnel token and save. "
    "This page picks up the change within 15 seconds; no restart needed."
)
SETUP_EXAMPLE = """# Cloudflare Zero Trust → Networks → Tunnels → Create a tunnel
# (remotely managed); copy the token shown after the tunnel name:
tunnel_token: eyJhIjoiYXBwLmtleSI6..."""


def _load_config(
    supervisor: Supervisor, secrets: SecretRefs, router: PanelRouter, stop: threading.Event
) -> tuple[dict[str, object], Config] | None:
    """The stored options (!secret references kept) and their config; waits in the panel while invalid."""

    raw_options = initial_options(load_options, supervisor.fetch_options, secrets)

    def validate(raw: dict[str, object]) -> Config:
        return load_config(secrets.resolve(raw))  # type: ignore[arg-type]

    try:
        return raw_options, validate(raw_options)
    except (ConfigError, SecretError) as err:
        _LOGGER.error("Configuration problem: %s", err)  # noqa: TRY400 - no traceback needed
        _LOGGER.info("Waiting for a valid configuration; open the Cloudflare Tunnel panel for details")
        view = SetupView("Cloudflare Tunnel", str(err), SETUP_HELP, SETUP_EXAMPLE)
        router.target = view
        result = wait_for_valid_config(supervisor.fetch_options, validate, view, stop)
        if result is None:
            return None
        _LOGGER.info("Configuration is valid now; starting")
        return result  # type: ignore[return-value]


def main() -> None:
    """Entry point."""

    merge_s6_environment()  # TZ first, so every log line uses local time
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    _LOGGER.info("Starting Cloudflare Tunnel %s", VERSION)
    version = cf.binary_version()
    if version:
        _LOGGER.info("cloudflared %s", version)

    token = load_supervisor_token()
    if not token:
        _LOGGER.error("No supervisor token; cannot reach Home Assistant")
        raise SystemExit(1)
    supervisor = Supervisor(token)

    stop = threading.Event()
    router = PanelRouter()

    def _stop(signum: int, _frame: FrameType | None) -> None:
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
        loaded = _load_config(supervisor, secrets, router, stop)
        if loaded is None:
            return
        raw_options, config = loaded
        if config.log_level != "info":
            _LOGGER.info("Log level set to %s", config.log_level)
        apply_log_level(config.log_level)
        store = Store()
        store.load()
        app = App(config, supervisor, store, raw_options)
        app.secrets = secrets
        if version:
            app.daemon.status.version = version
        router.target = app
        if stop.is_set():
            return
        app.run()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
