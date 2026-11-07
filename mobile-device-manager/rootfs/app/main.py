#!/usr/bin/env python3
"""Home Assistant app: Mobile Device Manager - inventory and safe cleanup of Companion-app devices."""

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

import inventory
from config import Config, ConfigError, load_config, load_options
from ha_ws import HaSocket, WsError
from inventory import Device
from store import Store
from supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
)
from webui import PanelError, PanelServer, action, badge, link, notice, stat, toggle

_LOGGER = logging.getLogger("mobile_device_manager")

VERSION = os.environ.get("MDM_VERSION", "dev")
LOOP_SECONDS = 60  # longest idle sleep; the loop wakes earlier for due work or panel actions
MIN_SLEEP_SECONDS = 1
OPTIONS_CHECK_SECONDS = 30
PHONE_CHECK_SECONDS = 300
STORE_SAVE_SECONDS = 60
FAILURES_BEFORE_NOTIFY = 3
WEB_DIR = Path(__file__).resolve().parent / "web"
NOTIFICATION_ID = "mobile_device_manager"
KIND_ICON = {"automation": "mdi:robot", "script": "mdi:script-text", "scene": "mdi:palette"}
KIND_DASHBOARD = {
    "automation": "/config/automation/dashboard",
    "script": "/config/script/dashboard",
    "scene": "/config/scene/dashboard",
}


def _ago(moment: datetime | None, now: datetime) -> str | None:
    if moment is None:
        return None
    seconds = max(0, int((now - moment).total_seconds()))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


class App:
    """Scans mobile_app devices on a schedule, serves the panel and removes devices on request."""

    def __init__(
        self, config: Config, supervisor: Supervisor, store: Store, raw_options: dict[str, object] | None = None
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}
        self.supervisor = supervisor
        self.store = store
        self.version = VERSION
        self.devices: list[Device] = []
        self.last_scan: datetime | None = None
        self.scan_error: str | None = None
        self.scan_failures = 0
        self.failure_notified = False
        self.next_scan = 0.0
        self.discovered: list[str] = []
        # entity_id -> (last_changed, stored config): a config is re-read only after it was edited/reloaded
        self.config_cache: dict[str, tuple[str, object]] = {}
        self.next_phone_check = 0.0
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None

    # -- Options (panel + Configuration tab, applied without a restart) -------------
    def view_config(self) -> Config:
        """Config the panel should show (a saved-but-not-yet-applied change wins)."""

        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        """Switch to new options in place (main thread)."""

        old = self.config
        self.config = config
        self.raw_options = raw
        if old.scan_minutes != config.scan_minutes:
            self.next_scan = min(self.next_scan, time.monotonic() + config.scan_minutes * 60)
        if (old.stale_days, old.ignored_devices) != (config.stale_days, config.ignored_devices):
            self._reflag()
        _LOGGER.info("Applied updated options")

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel; applied on the next loop pass (any thread)."""

        with self.options_lock:
            try:
                base = self.supervisor.fetch_options()
            except SupervisorError:
                base = self.raw_options
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(raw)
            except ConfigError as err:
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
        if raw != self.raw_options:
            self.apply_config(raw, load_config(raw))

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

        if self.config.notify_on_attention:
            targets = effective_targets(self.discovered, self.phones)
            self.supervisor.notify(targets, title, message, f"{NOTIFICATION_ID}{suffix}")

    # -- Scanning ------------------------------------------------------------------------
    def _configs(self, states: dict[str, dict[str, object]]) -> dict[str, object]:
        """Stored automation/script configs (UI-managed ones), keyed by entity id.

        Saving or reloading an automation/script recreates its state, which changes ``last_changed``;
        only those are fetched again, the rest come from the cache.
        """

        configs: dict[str, object] = {}
        cache: dict[str, tuple[str, object]] = {}
        for entity_id, state in states.items():
            domain, _, object_id = entity_id.partition(".")
            attrs = state.get("attributes") or {}
            if domain == "automation" and isinstance(attrs, dict) and attrs.get("id"):
                path = f"/config/automation/config/{attrs['id']}"
            elif domain == "script":
                path = f"/config/script/config/{object_id}"
            else:
                continue
            stamp = str(state.get("last_changed") or "")
            cached = self.config_cache.get(entity_id)
            if cached is not None and stamp and cached[0] == stamp:
                cache[entity_id] = cached
            else:
                try:
                    cache[entity_id] = (stamp, self.supervisor.core_get(path))
                except SupervisorError:
                    cache[entity_id] = (stamp, None)  # YAML-only: not readable through the config API
            if cache[entity_id][1] is not None:
                configs[entity_id] = cache[entity_id][1]
        self.config_cache = cache  # drops deleted automations/scripts
        return configs

    def _inventory(self, only: str | None = None) -> list[Device]:
        """Fetch registries, states and links; ``only`` limits the link search to one device id."""

        with HaSocket(self.supervisor.token) as ws:
            entries = ws.call("config_entries/get", domain=inventory.MOBILE_APP)
            devices_raw = ws.call("config/device_registry/list")
            entities_raw = ws.call("config/entity_registry/list")
            states_raw = self.supervisor.core_get("/states")
            services_raw = self.supervisor.core_get("/services")
            devices = inventory.build_devices(entries, devices_raw, entities_raw, states_raw, services_raw)
            if only is not None:
                devices = [d for d in devices if d.device_id == only]
            wanted = {e for d in devices for e in d.entity_ids}
            states = {  # keep only what links need: automations, scripts, scenes and the devices' entities
                str(s["entity_id"]): s
                for s in (states_raw if isinstance(states_raw, list) else [])
                if isinstance(s, dict)
                and (
                    str(s.get("entity_id", "")).split(".", 1)[0] in inventory.LINK_KINDS or s.get("entity_id") in wanted
                )
            }
            del devices_raw, entities_raw, states_raw, services_raw  # release the full lists early
            configs = self._configs(states) if devices else {}
            for device in devices:
                related = ws.call("search/related", item_type="device", item_id=device.device_id)
                device.links = inventory.merge_links(
                    inventory.links_from_related(related, states),
                    inventory.notify_links(device.notify_service, configs, states),
                )
        return devices

    def _reflag(self) -> None:
        inventory.apply_flags(self.devices, datetime.now(UTC), self.config.stale_days, set(self.config.ignored_devices))

    def scan(self) -> None:
        """Rebuild the inventory and send attention notifications."""

        try:
            devices = self._inventory()
        except (WsError, SupervisorError) as err:
            self.scan_failures += 1
            self.scan_error = str(err)
            _LOGGER.warning("Device scan failed (%d in a row): %s", self.scan_failures, err)
            if self.scan_failures >= FAILURES_BEFORE_NOTIFY and not self.failure_notified:
                self.failure_notified = True
                self.notify("Mobile Device Manager scan failing", f"Could not scan mobile devices: {err}", "_scan")
            return
        if self.failure_notified:
            self.notify("Mobile Device Manager scan recovered", "Mobile devices are being scanned again.", "_scan")
        self.scan_failures = 0
        self.failure_notified = False
        self.scan_error = None
        self.devices = devices
        self.last_scan = datetime.now(UTC)
        self._reflag()
        self._notify_stale()
        _LOGGER.info(
            "Scanned %d mobile device(s): %d stale, %d duplicate, %d in use by automations/scripts",
            len(devices),
            sum(inventory.FLAG_STALE in d.flags for d in devices),
            sum(inventory.FLAG_DUPLICATE in d.flags for d in devices),
            sum(bool(d.links) for d in devices),
        )

    def _notify_stale(self) -> None:
        notified = self.store.section("stale_notified")
        stale = {d.device_id: d for d in self.devices if inventory.FLAG_STALE in d.flags}
        new = [d for device_id, d in stale.items() if device_id not in notified]
        for device_id in [k for k in notified if k not in stale]:
            del notified[device_id]
        for device in new:
            notified[device.device_id] = True
        if new or notified.keys() != stale.keys():
            self.store.dirty = True
        if new:
            names = ", ".join(d.name for d in new)
            self.notify(
                "Mobile devices not seen recently",
                f"Not seen for over {self.config.stale_days} days: {names}. Review them in Mobile Device Manager.",
                "_stale",
            )

    # -- Removal (never forced) -------------------------------------------------------------
    def remove(self, device_id: str) -> str:
        """Remove a device's mobile_app registration after re-checking nothing uses it."""

        if not any(d.device_id == device_id for d in self.devices):
            raise PanelError("That device is no longer listed; reload the page")
        try:
            current = self._inventory(only=device_id)
        except (WsError, SupervisorError) as err:
            raise PanelError(f"Could not re-check the device before removing it: {err}") from err
        if not current:
            self.next_scan = 0.0
            self.wake.set()
            return "That device is already gone"
        device = current[0]
        if device.links:
            names = ", ".join(link.name for link in device.links[:5])
            raise PanelError(
                f"'{device.name}' is still used by {inventory.link_summary(device.links)} ({names}). "
                "Edit them first; devices in use are never removed."
            )
        try:
            self.supervisor.core_delete(f"/config/config_entries/entry/{device.entry_id}")
        except SupervisorError as err:
            raise PanelError(f"Home Assistant refused to remove '{device.name}': {err}") from err
        _LOGGER.info("Removed mobile device '%s' (its Companion app registration and entities)", device.name)
        self.devices = [d for d in self.devices if d.device_id != device_id]
        self.next_scan = 0.0
        self.wake.set()
        return f"Removed {device.name}"

    # -- Panel callbacks (HTTP thread) ---------------------------------------------------
    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        """Persist and apply one panel switch."""

        kind, _, name = toggle_id.partition(":")
        if kind == "phone" and name:
            self.set_phone(name, value)
            return
        if toggle_id == "notify_on_attention":
            self.update_options(lambda raw: raw.__setitem__("notify_on_attention", value))
            return
        raise PanelError("Unknown setting")

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Handle a panel button."""

        device_id = str(args.get("device") or "")
        if action_id == "rescan":
            self.next_scan = 0.0
            self.wake.set()
            return "Scanning..."
        if action_id == "remove":
            return self.remove(device_id)
        if action_id == "ignore":
            ignore = bool(args.get("value", True))

            def mutate(raw: dict[str, object]) -> None:
                current = {str(v) for v in raw.get("ignored_devices") or []}  # type: ignore[union-attr]
                current = current | {device_id} if ignore else current - {device_id}
                raw["ignored_devices"] = sorted(current)

            self.update_options(mutate)
            return "Ignored for stale warnings" if ignore else "Stale warnings back on"
        raise PanelError("Unknown action")

    # -- Panel view model ------------------------------------------------------------------
    @staticmethod
    def _badge(device: Device, ignored: bool) -> dict[str, str]:
        flags = device.flags
        if inventory.FLAG_NOT_LOADED in flags:
            return badge("Not loaded", "error")
        if inventory.FLAG_DUPLICATE in flags:
            return badge("Duplicate", "warn")
        if inventory.FLAG_STALE in flags:
            return badge("Stale", "warn")
        return badge("Ignored") if ignored else badge("Active", "ok")

    @staticmethod
    def _links(device: Device) -> list[dict[str, object]]:
        rows = []
        for item in device.links:
            reasons = []
            if "device" in item.reasons:
                reasons.append("uses this device or its sensors")
            if "notify" in item.reasons:
                reasons.append(f"sends notifications to notify.{device.notify_service}")
            description = "; ".join(reasons)
            if item.path is None:
                description += " (defined in YAML; edit it in your configuration files)"
            rows.append(
                link(
                    f"{item.kind.title()}: {item.name}",
                    item.path or KIND_DASHBOARD[item.kind],
                    description,
                    button="Edit" if item.path else "Open list",
                    icon=KIND_ICON[item.kind],
                )
            )
        return rows

    @staticmethod
    def _notice(device: Device, config: Config) -> dict[str, str] | None:
        flags = device.flags
        if device.links:
            return notice(
                f"Used by {inventory.link_summary(device.links)}. Edit them to stop using this phone; "
                "it can only be removed once nothing uses it."
            )
        if inventory.FLAG_DUPLICATE in flags:
            return notice("Looks like an older registration of the same phone. Safe to remove if unused.")
        if inventory.FLAG_STALE in flags:
            return notice(f"Not seen for over {config.stale_days} days.")
        if inventory.FLAG_NOT_LOADED in flags:
            return notice(f"The Companion app registration is '{device.entry_state}'.", "error")
        return None

    def _device_card(self, device: Device, config: Config, now: datetime) -> dict[str, object]:
        ignored = device.device_id in config.ignored_devices
        chip = self._badge(device, ignored)
        links = self._links(device)
        message = self._notice(device, config)

        subtitle = " · ".join(
            p for p in (" ".join(filter(None, (device.manufacturer, device.model))), device.os_version) if p
        )
        card: dict[str, object] = {
            "title": device.name,
            "icon": "mdi:cellphone",
            "subtitle": subtitle or "Companion app",
            "badge": chip,
            "stats": [
                stat("Last seen", _ago(device.last_seen, now) or "never"),
                stat("Battery", device.battery),
                stat("Notify service", device.notify_service or "none"),
                stat("Used by", len(device.links)),
            ],
            "links": links,
            "actions": [
                link(
                    "Open device",
                    f"/config/devices/device/{device.device_id}",
                    button="Open device",
                    icon="mdi:open-in-new",
                ),
                action(
                    "ignore",
                    "Unignore" if ignored else "Ignore",
                    {"device": device.device_id, "value": not ignored},
                    icon="mdi:eye" if ignored else "mdi:eye-off",
                ),
                action(
                    "remove",
                    "Remove",
                    {"device": device.device_id},
                    disabled=not device.removable,
                    danger=True,
                    confirm=(
                        f"Remove '{device.name}' from Home Assistant? This deletes its Companion app registration "
                        "and all of its entities. The phone can be added again from the Companion app."
                    ),
                    reason="Still used by automations or scripts; edit them first",
                    icon="mdi:delete",
                ),
            ],
        }
        if message:
            card["notice"] = message
        return card

    def panel_state(self) -> dict[str, object]:
        """The panel view model."""

        config = self.view_config()
        now = datetime.now(UTC)
        devices = list(self.devices)
        count = {
            flag: sum(flag in d.flags for d in devices)
            for flag in (inventory.FLAG_STALE, inventory.FLAG_DUPLICATE, inventory.FLAG_NOT_LOADED)
        }
        next_in = max(0, int(self.next_scan - time.monotonic())) // 60 if self.last_scan else None
        overview: dict[str, object] = {
            "title": "Overview",
            "icon": "mdi:view-dashboard",
            "subtitle": "Phones and tablets registered through the Home Assistant Companion app",
            "badge": badge("Scan failing", "error") if self.scan_error else badge(f"{len(devices)} device(s)", "ok"),
            "stats": [
                stat("Devices", len(devices)),
                stat("Stale", count[inventory.FLAG_STALE]),
                stat("Duplicates", count[inventory.FLAG_DUPLICATE]),
                stat("Not loaded", count[inventory.FLAG_NOT_LOADED]),
                stat("In use", sum(bool(d.links) for d in devices)),
                stat("Last scan", _ago(self.last_scan, now) or "scanning..."),
                stat("Next scan", f"in {next_in} min" if next_in is not None else None),
            ],
            "actions": [action("rescan", "Rescan now", icon="mdi:refresh")],
        }
        if self.scan_error:
            overview["notice"] = notice(f"Last scan failed: {self.scan_error}", "error")

        settings = {
            "title": "Settings",
            "icon": "mdi:cog",
            "subtitle": "Edit the scan interval, stale threshold and ignored devices in the Configuration tab",
            "stats": [stat("Stale after", config.stale_days, "days"), stat("Scan every", config.scan_minutes, "min")],
            "toggles": [
                toggle(
                    "notify_on_attention",
                    "Attention notifications",
                    config.notify_on_attention,
                    "Notify the phones below when a device turns stale or scanning keeps failing",
                    icon="mdi:bell",
                )
            ],
        }
        phones = {
            "title": "Phones",
            "icon": "mdi:cellphone-message",
            "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
            "toggles": [
                toggle(
                    f"phone:{name}",
                    name.removeprefix("mobile_app_").replace("_", " "),
                    self.phones.get(name, True) is not False,
                    name,
                    disabled=not config.notify_on_attention,
                    icon="mdi:cellphone",
                )
                for name in sorted(self.discovered)
            ],
            "empty": "No mobile apps found. Without phones a persistent notification is used.",
        }
        return {
            "title": "Mobile Device Manager",
            "subtitle": f"Version {self.version} · {len(devices)} device(s)",
            "sections": [
                {"title": "Summary", "icon": "mdi:view-dashboard", "cards": [overview]},
                {
                    "title": "Devices",
                    "icon": "mdi:cellphone-cog",
                    "cards": [self._device_card(d, config, now) for d in devices],
                },
                {"title": "Settings", "icon": "mdi:cog", "cards": [settings, phones]},
            ],
        }

    # -- Loop ----------------------------------------------------------------------------------
    def _tick(self) -> None:
        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        now = time.monotonic()
        if now >= self.next_options_check:
            self.next_options_check = now + OPTIONS_CHECK_SECONDS
            self.check_options()
        if now >= self.next_phone_check:
            self.next_phone_check = now + PHONE_CHECK_SECONDS
            self.refresh_phones()
        if now >= self.next_scan:
            self.next_scan = now + self.config.scan_minutes * 60
            self.scan()

    def run(self) -> None:
        """Serve the panel and run the loop until stopped."""

        server = PanelServer(WEB_DIR, self.panel_state, self.panel_toggle, self.panel_action)
        server.start()
        last_save = time.monotonic()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                self._tick()
                if time.monotonic() - last_save >= STORE_SAVE_SECONDS:
                    self.store.save()
                    last_save = time.monotonic()
                deadline = min(self.next_scan, self.next_options_check, self.next_phone_check)
                self.wake.wait(min(max(deadline - time.monotonic(), MIN_SLEEP_SECONDS), LOOP_SECONDS))
        finally:
            server.stop()
            self.store.save()
            _LOGGER.info("Stopped")


def main() -> None:
    """Entry point."""

    merge_s6_environment()  # TZ first, so every log line uses local time
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    _LOGGER.info("Starting Mobile Device Manager %s", VERSION)
    try:
        raw_options = load_options()
        config = load_config(raw_options)
    except ConfigError:
        _LOGGER.exception("Invalid configuration")
        raise SystemExit(1) from None

    token = load_supervisor_token()
    if not token:
        _LOGGER.error("No supervisor token; cannot reach Home Assistant")
        raise SystemExit(1)

    store = Store()
    store.load()
    app = App(config, Supervisor(token), store, raw_options)

    def _stop(signum: int, _frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %d; shutting down", signum)
        app.stopping.set()
        app.wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    app.run()


if __name__ == "__main__":
    main()
