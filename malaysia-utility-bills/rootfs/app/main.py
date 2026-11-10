#!/usr/bin/env python3
"""Home Assistant app: Malaysia Utility Bills - TNB electricity and Ranhill SAJ water in one app."""

from __future__ import annotations

import copy
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import panel
from config import PROVIDERS, PROVIDERS_BY_KEY, Config, ProviderSpec, import_options, load_config
from core import mqtt
from core.options import ConfigError, apply_log_level, load_options
from core.scoped import ScopedStore
from core.store import Store
from core.supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
)
from core.webui import (
    PanelError,
    PanelRouter,
    PanelServer,
    SecretError,
    SecretRefs,
    SetupView,
    initial_options,
    wait_for_valid_config,
)
from providers.base import Engine

_LOGGER = logging.getLogger("malaysia_utility_bills")

VERSION = os.environ.get("MALAYSIA_UTILITY_BILLS_VERSION", "dev")
APP_NAME = "Malaysia Utility Bills"
LOOP_SECONDS = 60  # longest idle sleep; the loop wakes earlier for due work or panel actions
MIN_SLEEP_SECONDS = 1
STORE_SAVE_SECONDS = 60
OPTIONS_CHECK_SECONDS = 30
MQTT_CHECK_SECONDS = 60
PHONE_CHECK_SECONDS = 300
OLD_APP_CHECK_SECONDS = 300
STATES_REFRESH_SECONDS = 300
SHARED_TOGGLES = ("mqtt", "notify_on_failure", "notify_on_unpaid")
WEB_DIR = Path(__file__).resolve().parent / "web"


class App:
    """Shared plumbing (options, phones, MQTT or the states API, the panel) around one engine per provider."""

    def __init__(
        self,
        config: Config,
        supervisor: Supervisor,
        store: Store,
        raw_options: dict[str, object] | None = None,
        engine_kwargs: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}  # as stored: !secret references, never their values
        self.secrets = SecretRefs()
        self.version = VERSION
        self.supervisor = supervisor
        self.store = store
        self.engine_kwargs = engine_kwargs or {}  # per provider, e.g. a fake site client in tests
        self.publisher: mqtt.Publisher | None = None
        self.broker_key: tuple[object, ...] | None = None
        self.broker_found: bool | None = None  # None until the first check
        self.next_mqtt_check = 0.0
        self.next_states_refresh = 0.0
        self.next_phone_check = 0.0
        self.next_old_app_check = 0.0
        self.states_warned = False
        self.posted: dict[str, dict[str, object]] = {}  # last body posted per entity (states-API sink)
        self.republish = threading.Event()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None
        self.discovered: list[str] = []
        self.old_apps: dict[str, dict[str, object]] = {}  # provider key -> the installed app it replaces
        self.running_old: set[str] = set()  # providers whose old app is running: hands off their entities
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.engines: dict[str, Engine] = {}
        self._reconcile(config)

    # -- Engines ----------------------------------------------------------------
    def _reconcile(self, config: Config) -> None:
        """Create, update or drop each provider's engine for ``config``."""

        for spec in PROVIDERS:
            provider_config = config.providers.get(spec.key)
            engine = self.engines.get(spec.key)
            if provider_config is None:
                if engine is not None:
                    del self.engines[spec.key]
                    _LOGGER.info("%s has no accounts now; stopped it", spec.label)
            elif engine is None:
                self.engines[spec.key] = spec.engine(
                    self, provider_config, ScopedStore(self.store, spec.key), **self.engine_kwargs.get(spec.key, {})
                )
            else:
                engine.reconcile(provider_config)
        self.config = config

    def active_engines(self) -> list[Engine]:
        """Engines that run now: a provider waits while the app it replaces is still running."""

        return [engine for key, engine in self.engines.items() if key not in self.running_old]

    def blocked(self) -> list[ProviderSpec]:
        """Configured providers that wait for their old app to stop."""

        return [spec for spec in PROVIDERS if spec.key in self.engines and spec.key in self.running_old]

    def _protected_topic(self, topic: str) -> bool:
        """A discovery topic a running old app still owns (never cleared from here)."""

        return any(PROVIDERS_BY_KEY[key].engine.owns_topic(topic) for key in self.running_old)

    def _protected_entity(self, entity_id: str) -> bool:
        """A states-API entity a running old app still owns (never deleted from here)."""

        return any(PROVIDERS_BY_KEY[key].engine.owns_entity(entity_id) for key in self.running_old)

    # -- Host API for the engines -------------------------------------------------
    def busy(self) -> bool:
        """Stop slow work now: shutting down, or a panel change waits to be applied."""

        return self.stopping.is_set() or self.pending is not None

    def use_states_api(self) -> bool:
        """Whether entities go through the Home Assistant states API right now."""

        return self.publisher is None and (self.broker_found is not None or not self.config.mqtt)

    def notify_targets(self) -> list[str]:
        """Linked phones that are switched on."""

        return effective_targets(self.discovered, self.phones)

    def notify(self, notification_id: str, title: str, message: str, *, bill: bool = False) -> None:
        """Send a notification when that kind is switched on (``bill``: an unpaid-bill notice)."""

        if self.config.notify_on_unpaid if bill else self.config.notify_on_failure:
            self.supervisor.notify(self.notify_targets(), title, message, notification_id)

    def post_state(self, entity_id: str, state: str, attributes: dict[str, object]) -> bool:
        """Set one entity through the states API (skipped when unchanged); False when it failed."""

        body = {"state": state, "attributes": attributes}
        if self.posted.get(entity_id) == body:
            return True  # unchanged: skip the HTTP round trip
        try:
            self.supervisor.core_post(f"/states/{entity_id}", body)
        except SupervisorError as err:
            if not self.states_warned:
                self.states_warned = True
                _LOGGER.warning("Could not update entities through the states API: %s", err)
            return False
        self.states_warned = False
        self.posted[entity_id] = copy.deepcopy(body)
        return True

    # -- Options (panel toggles + Configuration tab, applied without a restart) --
    def view_config(self) -> Config:
        """Config the panel should show (a saved-but-not-yet-applied change wins)."""

        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        """Switch to new options in place (main thread only)."""

        old = self.config
        self._reconcile(config)
        self.raw_options = raw
        if old.mqtt != config.mqtt:
            self.next_mqtt_check = 0.0
        if old.log_level != config.log_level:
            _LOGGER.info("Log level set to %s", config.log_level)  # before applying: may hide info
            apply_log_level(config.log_level)
        self.publish_discovery()
        _LOGGER.info("Applied updated options (%d account(s))", config.account_count)

    def update_options(
        self, mutate: Callable[[dict[str, object]], None], *, allow_unresolved: bool = False
    ) -> str | None:
        """Change the saved options from the panel and apply them right away (any thread).

        With ``allow_unresolved`` options naming a ``!secret`` this run does not know are still
        saved (applied after a restart); the returned text says so.
        """

        with self.options_lock:
            try:
                base = self.supervisor.fetch_options()
            except SupervisorError as err:
                # Never fall back to the start-up copy: it holds resolved !secret values.
                raise PanelError("Home Assistant is not reachable; try again") from err
            raw = copy.deepcopy(base)
            mutate(raw)
            config: Config | None = None
            try:
                config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
            except SecretError as err:
                if not allow_unresolved:
                    raise PanelError(str(err)) from err
            except ConfigError as err:
                raise PanelError(str(err)) from err
            try:
                self.supervisor.write_options(raw)
            except SupervisorError as err:
                raise PanelError("Home Assistant rejected the change; see the app log") from err
            if config is None:
                return "Saved. The options use a !secret this run does not know yet: restart the app to apply them"
            self.pending = (raw, config)
        self.wake.set()
        return None

    def check_options(self) -> None:
        """Pick up edits made in the Configuration tab (polled)."""

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

    # -- The apps this one replaces ---------------------------------------------
    def check_old_apps(self) -> None:
        """Find the apps this one replaces; a configured provider waits while its old app runs.

        Both would write the same MQTT topics and entity ids, so only one may run.
        """

        try:
            addons = self.supervisor.list_addons()
        except SupervisorError as err:
            _LOGGER.debug("Could not list the installed apps: %s", err)
            return  # keep the last known state
        found: dict[str, dict[str, object]] = {}
        for spec in PROVIDERS:
            for addon in addons:
                slug = str(addon.get("slug") or "")
                if slug == spec.old_slug or slug.endswith(f"_{spec.old_slug}"):
                    found[spec.key] = addon
                    break
        running = {key for key, addon in found.items() if addon.get("state") == "started"}
        for key in sorted(running - self.running_old):
            spec = PROVIDERS_BY_KEY[key]
            if key in self.engines:
                _LOGGER.warning(
                    "%s is still running: %s waits until it is stopped (both would write the same entities)",
                    spec.old_name,
                    spec.label,
                )
        for key in sorted(self.running_old - running):
            spec = PROVIDERS_BY_KEY[key]
            if key in self.engines:
                _LOGGER.info("%s is stopped; %s takes over its entities now", spec.old_name, spec.label)
            self.republish.set()
        self.old_apps = found
        self.running_old = running

    def import_old_options(self, key: str) -> str:
        """Copy the options of the app a provider replaces into this app's ``<provider>_`` options."""

        spec = PROVIDERS_BY_KEY.get(key)
        if spec is None:
            raise PanelError("Unknown provider")
        try:
            addon = self.supervisor.find_addon(spec.old_slug)
        except SupervisorError as err:
            raise PanelError("Home Assistant is not reachable; try again") from err
        if addon is None:
            raise PanelError(f"{spec.old_name} is not installed")
        try:
            old = self.supervisor.addon_options(str(addon.get("slug")))
        except SupervisorError as err:
            raise PanelError(f"Could not read the options of {spec.old_name}: {err}") from err
        imported = import_options(spec, old)
        accounts = imported.get(f"{spec.prefix}accounts")
        if not isinstance(accounts, list) or not accounts:
            raise PanelError(f"{spec.old_name} has no accounts to import")

        def mutate(raw: dict[str, object]) -> None:
            if raw.get(f"{spec.prefix}accounts"):
                raise PanelError(f"{spec.prefix}accounts already lists accounts; remove them first to import")
            raw.update(imported)

        note = self.update_options(mutate, allow_unresolved=True)
        _LOGGER.info("Imported %d %s account(s) from %s", len(accounts), spec.label, spec.old_name)
        done = f"Imported {len(accounts)} account(s) from {spec.old_name}."
        if note:
            return f"{done} {note}"
        if addon.get("state") == "started":
            return f"{done} Stop {spec.old_name} so this app can take over its entities."
        return done

    # -- Panel callbacks (HTTP thread) -------------------------------------------
    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        """Persist and apply one panel switch."""

        kind, _, rest = toggle_id.partition(":")
        if kind == "phone" and rest:
            self.set_phone(rest, value)
            return
        if kind in SHARED_TOGGLES and not rest:
            self.update_options(lambda raw: raw.__setitem__(kind, value))
            return
        spec = PROVIDERS_BY_KEY.get(kind)
        if spec is None or not rest:
            raise PanelError("Unknown setting")
        self.update_options(lambda raw: spec.apply_toggle(raw, rest, value, spec.prefix))

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Handle a panel button (``<provider>:<action>`` or ``import``)."""

        if action_id == "import":
            return self.import_old_options(str(args.get("provider") or ""))
        kind, _, rest = action_id.partition(":")
        engine = self.engines.get(kind)
        if engine is None or not rest:
            raise PanelError("Unknown action")
        if kind in self.running_old:
            raise PanelError(f"Stop {PROVIDERS_BY_KEY[kind].old_name} first; this app waits until then")
        return engine.panel_action(rest, args)

    def panel_state(self) -> dict[str, object]:
        """The panel view model."""

        return panel.build_state(self)

    # -- Notifications ----------------------------------------------------------
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
            current = self.phones
            merged = merge_discovered(current, found)
            added = sorted(set(merged) - set(current))
            removed = sorted(set(current) - set(merged))
            first = not self.discovered and not added and not removed
            self.discovered = found
            if merged != current:
                self.store.data["phones"] = merged
                self.store.dirty = True
        if added:
            _LOGGER.info("Found phone(s): %s", ", ".join(added))
        if removed:
            _LOGGER.info("Phone(s) no longer linked: %s", ", ".join(removed))
        if added or removed or first:
            targets = self.notify_targets()
            _LOGGER.info("Notifications go to %d phone(s): %s", len(targets), ", ".join(targets) or "-")

    def set_phone(self, name: str, value: bool) -> None:
        """Panel switch for one phone (kept in /data, applies immediately)."""

        with self.options_lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            self.store.dirty = True

    # -- MQTT broker (optional, detected while running) -------------------------
    def mqtt_status(self) -> str:
        """How entities currently reach Home Assistant (for the panel)."""

        if self.publisher is not None:
            return "MQTT (connected)" if self.publisher.connected else "MQTT (connecting)"
        if not self.config.mqtt:
            return "HA states API (MQTT off)"
        if self.broker_found is None:
            return "Checking for MQTT..."
        return "HA states API (no MQTT broker)"

    def check_mqtt(self) -> None:
        """Use MQTT when it is switched on and a broker exists; otherwise fall back to the states API."""

        if not self.config.mqtt:
            if self.publisher is not None:
                self._retire_mqtt()
                _LOGGER.info("MQTT switched off; entities now go through the Home Assistant states API")
                # Give Home Assistant a moment to drop the MQTT entities before reusing their ids.
                self.next_states_refresh = time.monotonic() + 10
            return
        try:
            broker = self.supervisor.mqtt_service()
        except SupervisorError as err:
            _LOGGER.warning("Could not check for an MQTT broker: %s", err)
            return  # keep whatever is in use
        found = broker is not None
        if found != self.broker_found and broker is None:
            _LOGGER.warning(
                "No MQTT broker found (install the Mosquitto broker app to get MQTT devices); "
                "publishing entities through the Home Assistant states API and checking again every minute"
            )
        self.broker_found = found
        if broker is None:
            return  # an existing publisher keeps reconnecting, so entity ids never flip sinks
        key = tuple(broker.get(k) for k in ("host", "port", "username", "password", "ssl"))
        if self.publisher is not None and key == self.broker_key:
            return
        if self.publisher is not None:
            _LOGGER.info("MQTT broker settings changed; reconnecting")
            self.publisher.stop()
        else:
            _LOGGER.info("MQTT broker found; switching entities to MQTT discovery")
            self._delete_rest_entities()
        self.broker_key = key
        self.publisher = mqtt.Publisher(broker, self._request_republish)
        self.publisher.start()
        self.republish.set()

    def _request_republish(self) -> None:
        """Called from the MQTT thread (connect / Home Assistant online): republish on the main loop now."""

        self.republish.set()
        self.wake.set()

    def _retire_mqtt(self) -> None:
        """Remove retained discovery configs (not those a running old app owns) and disconnect."""

        publisher = self.publisher
        if publisher is None:
            return
        topics = self.store.data.get("discovery_topics")
        kept: list[str] = []
        for topic in topics if isinstance(topics, list) else []:
            if self._protected_topic(str(topic)):
                kept.append(str(topic))
            else:
                publisher.publish_json(topic, None)
        self.store.data["discovery_topics"] = kept
        self.store.dirty = True
        publisher.stop()
        self.publisher = None
        self.broker_key = None

    # -- Home Assistant states API (used while there is no MQTT) ------------------
    def _rest_entities(self) -> list[str]:
        value = self.store.data.get("rest_entities")
        return [str(v) for v in value] if isinstance(value, list) else []

    def _sync_rest_entities(self) -> None:
        """Post every entity through the states API and drop ones that no longer exist."""

        current = [entity_id for engine in self.active_engines() for entity_id in engine.entity_ids()]
        for stale in set(self._rest_entities()) - set(current):
            if self._protected_entity(stale):
                continue
            try:
                self.supervisor.core_delete(f"/states/{stale}")
            except SupervisorError:
                continue
        if self.store.data.get("rest_entities") != current:
            self.store.data["rest_entities"] = current
            self.store.dirty = True
        self.posted.clear()  # full re-post: states set through the API vanish when Home Assistant restarts
        for engine in self.active_engines():
            engine.publish_all()
        self.next_states_refresh = time.monotonic() + STATES_REFRESH_SECONDS

    def _delete_rest_entities(self) -> None:
        """Remove states-API entities so MQTT discovery can own the same entity ids."""

        for entity_id in self._rest_entities():
            if self._protected_entity(entity_id):
                continue
            try:
                self.supervisor.core_delete(f"/states/{entity_id}")
            except SupervisorError as err:
                _LOGGER.warning("Could not remove %s: %s", entity_id, err)
        self.store.data["rest_entities"] = []
        self.posted.clear()
        self.store.dirty = True

    # -- Publishing -------------------------------------------------------------
    def publish_discovery(self) -> None:
        """Publish every entity: MQTT discovery when connected to a broker, else the states API."""

        if self.publisher is None:
            if self.use_states_api():
                self._sync_rest_entities()
            return
        published: list[str] = []
        for engine in self.active_engines():
            for topic, payload in engine.discovery():
                self.publisher.publish_json(topic, payload)
                published.append(topic)
        previous = self.store.data.get("discovery_topics")
        previous_topics = {str(t) for t in previous} if isinstance(previous, list) else set()
        protected = {t for t in previous_topics if self._protected_topic(t)}
        for stale in previous_topics - set(published) - protected:
            self.publisher.publish_json(stale, None)
            _LOGGER.info("Removed a stale entity from MQTT discovery")
        self.store.data["discovery_topics"] = published + sorted(protected - set(published))
        self.store.dirty = True
        for engine in self.active_engines():
            engine.publish_all()

    # -- Loop -------------------------------------------------------------------
    def _tick(self) -> None:
        """One pass of the scheduler: options, MQTT, phones, old apps, then each provider's due work."""

        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        if time.monotonic() >= self.next_options_check:
            self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
            self.check_options()
        now = time.monotonic()
        if now >= self.next_old_app_check:
            self.next_old_app_check = now + OLD_APP_CHECK_SECONDS
            self.check_old_apps()
        if now >= self.next_mqtt_check:
            self.next_mqtt_check = now + MQTT_CHECK_SECONDS
            self.check_mqtt()
            if self.publisher is None and self.next_states_refresh == 0.0:
                self.republish.set()  # first pass without a broker: create the entities
        if now >= self.next_phone_check:
            self.next_phone_check = now + PHONE_CHECK_SECONDS
            self.refresh_phones()
        if self.publisher is None and self.next_states_refresh and now >= self.next_states_refresh:
            self.republish.set()  # states-API entities vanish when Home Assistant restarts

        if self.republish.is_set():
            self.republish.clear()
            self.publish_discovery()

        for engine in self.active_engines():
            if self.busy():
                break  # apply a panel change before doing more slow work
            engine.tick()

    def _idle_seconds(self) -> float:
        """Sleep until the next scheduled job, at most LOOP_SECONDS."""

        deadlines = [self.next_options_check, self.next_mqtt_check, self.next_phone_check, self.next_old_app_check]
        if self.next_states_refresh:
            deadlines.append(self.next_states_refresh)
        for engine in self.active_engines():
            deadlines += engine.deadlines()
        wait = min(deadlines, default=time.monotonic() + LOOP_SECONDS) - time.monotonic()
        return min(max(wait, MIN_SLEEP_SECONDS), LOOP_SECONDS)

    def run(self) -> None:
        """Main scheduling loop (the panel server is owned by ``main``)."""

        last_save = time.monotonic()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                self._tick()
                if time.monotonic() - last_save >= STORE_SAVE_SECONDS:
                    self.store.save()
                    last_save = time.monotonic()
                self.wake.wait(self._idle_seconds())
        finally:
            self.store.save()
            if self.publisher is not None:
                self.publisher.stop()
            _LOGGER.info("Stopped")


SETUP_HELP = (
    f"Open Settings → Apps → {APP_NAME} → Configuration, fix the option named above and save. "
    "This page picks up the change within 15 seconds; no restart needed."
)
SETUP_EXAMPLE = """tnb_mytnb_username: you@example.com
tnb_mytnb_password: your-myTNB-password
tnb_accounts:
  - name: Home
    account_number: "123456789012"
    tariff: general
    billing_start_day: 1
saj_accounts:
  - name: Home
    account_number: "12345678-A1234567\""""


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
        _LOGGER.info("Waiting for a valid configuration; open the Utility Bills panel for details")
        view = SetupView(APP_NAME, str(err), SETUP_HELP, SETUP_EXAMPLE)
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
    _LOGGER.info("Starting %s %s", APP_NAME, VERSION)

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
        for engine in app.engines.values():
            engine.log_accounts()
        if not app.engines:
            _LOGGER.warning("No accounts configured yet; open the Utility Bills panel to add or import them")
        router.target = app
        if stop.is_set():
            return
        app.run()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
