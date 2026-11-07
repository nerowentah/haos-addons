#!/usr/bin/env python3
"""Home Assistant app: Power TNB (Malaysia) - multi-account usage and bill estimates."""

from __future__ import annotations

import copy
import logging
import os
import random
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import FrameType

import mqtt_pub
import panel
import stats
import tariff
from config import Account, Config, ConfigError, load_config, load_options
from holidays import HolidayCalendar
from meter import MYT, AccountMeter, billing_period_start, is_peak, to_kwh
from mytnb import AuthError, MyTnbError, MyTnbSession, PortalData
from store import Store
from supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
    notification_id,
    overrides_from_options,
)
from webui import PanelError, PanelServer

_LOGGER = logging.getLogger("power_tnb")

VERSION = os.environ.get("POWER_TNB_VERSION", "dev")
LOOP_SECONDS = 10
STORE_SAVE_SECONDS = 60
PORTAL_WINDOW_DAYS = 30
PORTAL_RETRY_SECONDS = 30 * 60
PORTAL_STAGGER_SECONDS = 20
TRANSIENT_FAILURES_BEFORE_NOTIFY = 3
OPTIONS_CHECK_SECONDS = 30
MQTT_CHECK_SECONDS = 60
PHONE_CHECK_SECONDS = 300
STATES_REFRESH_SECONDS = 300
WEB_DIR = Path(__file__).resolve().parent / "web"
UNAVAILABLE_STATES = {"unknown", "unavailable", "none", ""}

STATUS_OK = "ok"
STATUS_AUTH = "auth_error"
STATUS_ERROR = "error"


def cycle_start_iso(account: Account, now: datetime) -> str:
    """ISO start (MYT midnight) of the account's current billing cycle, used as last_reset."""

    start = billing_period_start(now.astimezone(MYT).date(), account.billing_start_day)
    return datetime.combine(start, datetime.min.time(), MYT).isoformat()


@dataclass(slots=True)
class AccountRuntime:
    """Live state for one account."""

    account: Account
    meter: AccountMeter | None = None
    portal: MyTnbSession | None = None
    values: dict[str, object] = field(default_factory=dict)
    bill_attributes: dict[str, object] = field(default_factory=dict)
    portal_attributes: dict[str, object] = field(default_factory=dict)
    next_meter: float = 0.0
    next_portal: float = 0.0
    portal_failures: int = 0
    failure_notified: bool = False
    missing_entities: set[str] = field(default_factory=set)


class App:
    """Scheduler that polls meters, syncs the portal and publishes entities (MQTT or the states API)."""

    def __init__(
        self, config: Config, supervisor: Supervisor, store: Store, raw_options: dict[str, object] | None = None
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}
        self.version = VERSION
        self.supervisor = supervisor
        self.store = store
        self.holidays = HolidayCalendar(config.extra_holidays, config.calendarific_api_key)
        self.publisher: mqtt_pub.Publisher | None = None
        self.broker_key: tuple[object, ...] | None = None
        self.broker_found: bool | None = None  # None until the first check
        self.next_mqtt_check = 0.0
        self.next_states_refresh = 0.0
        self.next_phone_check = 0.0
        self.states_warned = False
        self.republish = threading.Event()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None
        self.discovered: list[str] = []
        self.holiday_day: date | None = None
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.runtimes: list[AccountRuntime] = []
        self._reconcile(config)

    def _reconcile(self, config: Config) -> None:
        """(Re)build account runtimes for ``config``, keeping the live state of unchanged accounts."""

        old = {rt.account.account_id: rt for rt in self.runtimes}
        meters = self.store.section("meters")
        start = time.monotonic()
        runtimes: list[AccountRuntime] = []
        for index, account in enumerate(config.accounts):
            runtime = old.get(account.account_id)
            previous = runtime.account if runtime else None
            if runtime is None:
                runtime = AccountRuntime(account=account, values=self._blank_values(account))
            else:
                runtime.account = account
                for key, value in self._blank_values(account).items():
                    runtime.values.setdefault(key, value)

            if account.has_meter:
                if runtime.meter is None:
                    runtime.meter = AccountMeter.from_dict(meters.get(account.account_id), account.billing_start_day)  # type: ignore[arg-type]
                runtime.meter.billing_start_day = account.billing_start_day
            else:
                runtime.meter = None

            creds = (account.mytnb_username, account.mytnb_password, account.smartmeter_url)
            old_creds = (
                (previous.mytnb_username, previous.mytnb_password, previous.smartmeter_url) if previous else None
            )
            if not account.has_portal:
                runtime.portal = None
            elif runtime.portal is None or creds != old_creds:
                runtime.portal = MyTnbSession(*creds)
                runtime.next_portal = start + 5 + index * PORTAL_STAGGER_SECONDS
                runtime.portal_failures = 0
                runtime.failure_notified = False
                runtime.values["portal_status"] = "pending"
            elif previous is not None and not previous.portal_active and account.portal_active:
                runtime.next_portal = start + 5  # just switched on: sync soon
            runtimes.append(runtime)
        self.runtimes = runtimes
        self.config = config

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
        if (old.extra_holidays, old.calendarific_api_key) != (config.extra_holidays, config.calendarific_api_key):
            self.holidays = HolidayCalendar(config.extra_holidays, config.calendarific_api_key)
            self.holidays.refresh(datetime.now(MYT).year)
        if old.mqtt != config.mqtt:
            self.next_mqtt_check = 0.0
        self.publish_discovery()
        _LOGGER.info("Applied updated options (%d account(s))", len(config.accounts))

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel and apply them right away (any thread)."""

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
        """Pick up edits made in the Configuration tab (polled)."""

        try:
            raw = self.supervisor.fetch_options()
        except SupervisorError:
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

    # -- Panel callbacks (HTTP thread) -------------------------------------------
    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        """Persist and apply one panel switch."""

        kind, _, name = toggle_id.partition(":")
        if kind == "phone" and name:
            self.set_phone(name, value)
            return
        self.update_options(lambda raw: panel.apply_toggle(raw, toggle_id, value))

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Handle a panel button."""

        if action_id == "sync":
            account_id = str(args.get("account") or "")
            for runtime in self.runtimes:
                if runtime.account.account_id == account_id:
                    if not runtime.account.portal_active:
                        raise PanelError("myTNB sync is switched off for this account")
                    runtime.next_portal = 0.0
                    self.wake.set()
                    return f"Syncing myTNB for {runtime.account.name}..."
            raise PanelError("That account no longer exists; reload the page")
        raise PanelError("Unknown action")

    @staticmethod
    def _blank_values(account: Account) -> dict[str, object]:
        values: dict[str, object] = {sensor.key: None for sensor in mqtt_pub.sensors_for(account)}
        values["period_start"] = cycle_start_iso(account, datetime.now(MYT))
        if account.has_portal:
            values["portal_status"] = "pending"
        return values

    # -- Notifications ----------------------------------------------------------
    @property
    def phones(self) -> dict[str, object]:
        """Per-phone switches kept in /data (phone name -> notify)."""

        return self.store.section("phones")

    def migrate_overrides(self) -> None:
        """Move switches from the retired ``notification_overrides`` option into /data."""

        if "notification_overrides" not in self.raw_options:
            return
        old = overrides_from_options(self.raw_options)
        for name, notify in old.items():
            self.phones.setdefault(name, notify)
        self.store.dirty = True
        self.store.save()
        raw = {k: v for k, v in self.raw_options.items() if k != "notification_overrides"}
        try:
            self.supervisor.write_options(raw)
            self.raw_options = raw
            _LOGGER.info("Moved %d phone switch(es) out of the Configuration tab", len(old))
        except SupervisorError:
            _LOGGER.warning("Could not remove notification_overrides from the options; it is ignored")

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
            targets = effective_targets(self.discovered, self.phones)
            _LOGGER.info("Notifications go to %d phone(s): %s", len(targets), ", ".join(targets) or "-")

    def set_phone(self, name: str, value: bool) -> None:
        """Panel switch for one phone (kept in /data, applies immediately)."""

        with self.options_lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            self.store.dirty = True

    def notify(self, account: Account, title: str, message: str) -> None:
        """Send a notification for an account when enabled."""

        if self.config.notify_on_failure:
            targets = effective_targets(self.discovered, self.phones)
            self.supervisor.notify(targets, title, message, notification_id(account.account_id))

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
        self.publisher = mqtt_pub.Publisher(broker, self.republish.set)
        self.publisher.start()
        self.republish.set()

    def _retire_mqtt(self) -> None:
        """Remove retained discovery configs and disconnect."""

        publisher = self.publisher
        if publisher is None:
            return
        topics = self.store.data.get("discovery_topics")
        for topic in topics if isinstance(topics, list) else []:
            publisher.publish_json(topic, None)
        self.store.data["discovery_topics"] = []
        self.store.dirty = True
        publisher.stop()
        self.publisher = None
        self.broker_key = None

    # -- Home Assistant states API (used while there is no MQTT) ------------------
    def _rest_entities(self) -> list[str]:
        value = self.store.data.get("rest_entities")
        return [str(v) for v in value] if isinstance(value, list) else []

    def _post_state(self, entity_id: str, state: str, attributes: dict[str, object]) -> bool:
        try:
            self.supervisor.core_post(f"/states/{entity_id}", {"state": state, "attributes": attributes})
        except SupervisorError as err:
            if not self.states_warned:
                self.states_warned = True
                _LOGGER.warning("Could not update entities through the states API: %s", err)
            return False
        self.states_warned = False
        return True

    def _post_states(self, runtime: AccountRuntime) -> None:
        account = runtime.account
        active = {mqtt_pub.SOURCE_PORTAL: account.portal_active, mqtt_pub.SOURCE_METER: account.meter_active}
        extra = {"bill_estimate": runtime.bill_attributes, "portal_usage_latest_day": runtime.portal_attributes}
        for sensor in mqtt_pub.sensors_for(account):
            attributes = mqtt_pub.state_attributes(account, sensor)
            if sensor.last_reset_key and runtime.values.get(sensor.last_reset_key):
                attributes["last_reset"] = runtime.values[sensor.last_reset_key]
            attributes.update(extra.get(sensor.key) or {})
            value = mqtt_pub.state_value(sensor, runtime.values.get(sensor.key))
            state = value if active[sensor.source] else "unavailable"
            self._post_state(mqtt_pub.entity_id(account.account_id, sensor.key), state, attributes)

    def _post_holiday(self) -> None:
        today = datetime.now(MYT).date()
        self._post_state(
            mqtt_pub.HOLIDAY_ENTITY_ID,
            "on" if self.holidays.is_holiday(today) else "off",
            {"friendly_name": mqtt_pub.HOLIDAY_NAME, "icon": "mdi:calendar-star"},
        )

    def _sync_rest_entities(self) -> None:
        """Post every entity through the states API and drop ones that no longer exist."""

        current = [
            mqtt_pub.entity_id(rt.account.account_id, s.key)
            for rt in self.runtimes
            for s in mqtt_pub.sensors_for(rt.account)
        ]
        current.append(mqtt_pub.HOLIDAY_ENTITY_ID)
        for stale in set(self._rest_entities()) - set(current):
            try:
                self.supervisor.core_delete(f"/states/{stale}")
            except SupervisorError:
                continue
        self.store.data["rest_entities"] = current
        self.store.dirty = True
        for runtime in self.runtimes:
            self._post_states(runtime)
        self._post_holiday()
        self.next_states_refresh = time.monotonic() + STATES_REFRESH_SECONDS

    def _delete_rest_entities(self) -> None:
        """Remove states-API entities so MQTT discovery can own the same entity ids."""

        for entity_id in self._rest_entities():
            try:
                self.supervisor.core_delete(f"/states/{entity_id}")
            except SupervisorError as err:
                _LOGGER.warning("Could not remove %s: %s", entity_id, err)
        self.store.data["rest_entities"] = []
        self.store.dirty = True

    # -- MQTT -------------------------------------------------------------------
    def publish_discovery(self) -> None:
        """Publish every entity: MQTT discovery when connected to a broker, else the states API."""

        if self.publisher is None:
            if self.broker_found is not None or not self.config.mqtt:
                self._sync_rest_entities()
            return
        published: list[str] = []
        for runtime in self.runtimes:
            for sensor in mqtt_pub.sensors_for(runtime.account):
                topic = mqtt_pub.discovery_topic("sensor", f"{runtime.account.account_id}_{sensor.key}")
                self.publisher.publish_json(topic, mqtt_pub.sensor_config(runtime.account, sensor, VERSION))
                published.append(topic)
        holiday_topic = mqtt_pub.discovery_topic("binary_sensor", "holiday_today")
        self.publisher.publish_json(holiday_topic, mqtt_pub.holiday_config(VERSION))
        published.append(holiday_topic)

        previous = self.store.data.get("discovery_topics")
        for stale in set(previous if isinstance(previous, list) else []) - set(published):
            self.publisher.publish_json(stale, None)
            _LOGGER.info("Removed a stale entity from MQTT discovery")
        self.store.data["discovery_topics"] = published
        self.store.dirty = True

        for runtime in self.runtimes:
            self.publish_availability(runtime)
            self.publish_state(runtime)
        self.publish_holiday()

    def publish_availability(self, runtime: AccountRuntime) -> None:
        """Mark each source online/offline according to the panel switches."""

        if self.publisher is None:
            return
        account = runtime.account
        for source, configured, active in (
            (mqtt_pub.SOURCE_PORTAL, account.has_portal, account.portal_active),
            (mqtt_pub.SOURCE_METER, account.has_meter, account.meter_active),
        ):
            if configured:
                self.publisher.publish_raw(
                    mqtt_pub.source_availability_topic(account.account_id, source), "online" if active else "offline"
                )

    def publish_state(self, runtime: AccountRuntime) -> None:
        """Publish an account's state (and attributes) payloads."""

        if self.publisher is None:
            if self.broker_found is not None or not self.config.mqtt:
                self._post_states(runtime)
            return
        account_id = runtime.account.account_id
        self.publisher.publish_json(mqtt_pub.state_topic(account_id), runtime.values)
        if runtime.account.has_meter:
            self.publisher.publish_json(mqtt_pub.attributes_topic(account_id, "bill_estimate"), runtime.bill_attributes)
        if runtime.account.has_portal:
            self.publisher.publish_json(
                mqtt_pub.attributes_topic(account_id, "portal_usage_latest_day"), runtime.portal_attributes
            )

    def publish_holiday(self) -> None:
        """Publish whether today is an off-peak holiday."""

        if self.publisher is None and (self.broker_found is not None or not self.config.mqtt):
            self._post_holiday()
        if self.publisher is not None:
            today = datetime.now(MYT).date()
            self.publisher.publish_raw(
                f"{mqtt_pub.BASE_TOPIC}/holiday_today", "ON" if self.holidays.is_holiday(today) else "OFF"
            )

    # -- Meter ------------------------------------------------------------------
    def _read_kwh(self, runtime: AccountRuntime, entity_id: str) -> float | None:
        try:
            state = self.supervisor.get_state(entity_id)
        except SupervisorError as err:
            _LOGGER.warning("Could not read %s: %s", entity_id, err)
            return None
        if state is None:
            if entity_id not in runtime.missing_entities:
                _LOGGER.warning("Account '%s': entity %s does not exist", runtime.account.name, entity_id)
                runtime.missing_entities.add(entity_id)
            return None
        runtime.missing_entities.discard(entity_id)
        raw = str(state.get("state") or "").strip()
        if raw.lower() in UNAVAILABLE_STATES:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        attributes = state.get("attributes")
        unit = attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None
        return to_kwh(value, str(unit) if unit else None)

    def poll_meter(self, runtime: AccountRuntime) -> None:
        """Read the energy sensors, update buckets and recompute the estimate."""

        meter = runtime.meter
        account = runtime.account
        if meter is None:
            return
        now = datetime.now(MYT)
        import_kwh = self._read_kwh(runtime, account.import_entity)
        export_kwh = self._read_kwh(runtime, account.export_entity) if account.export_entity else None
        meter.roll_over(now, account.tariff, self.config.afa_rate)
        meter.ingest(now, import_kwh, export_kwh, self.holidays.is_holiday)
        self.store.section("meters")[account.account_id] = meter.to_dict()
        self.store.dirty = True

        bill = meter.bill(account.tariff, self.config.afa_rate)
        predicted = meter.predicted_bill(now, account.tariff, self.config.afa_rate)
        peak_now = account.tariff == tariff.TARIFF_TOU and is_peak(now, self.holidays.is_holiday)
        period_start = datetime.combine(date.fromisoformat(meter.period_start), datetime.min.time(), MYT)
        runtime.values.update(
            {
                "period_start": period_start.isoformat(),
                "bill_estimate": bill.total,
                "predicted_bill": predicted.total if predicted else None,
                "predicted_kwh": round(predicted.import_kwh, 1) if predicted else None,
                "cycle_import": round(bill.import_kwh, 3),
                "cycle_peak": round(meter.peak_kwh, 3),
                "cycle_offpeak": round(meter.offpeak_kwh, 3),
                "cycle_export": round(meter.export_kwh, 3),
                "today_import": round(meter.today_peak_kwh + meter.today_offpeak_kwh, 3),
                "today_export": round(meter.today_export_kwh, 3),
                "current_rate": tariff.marginal_rate(
                    account.tariff, bill.import_kwh, peak=peak_now, afa_rate=self.config.afa_rate
                ),
                "current_period": ("peak" if peak_now else "off-peak")
                if account.tariff == tariff.TARIFF_TOU
                else "flat",
                "tier": bill.tier,
                "days_remaining": meter.days_remaining(now),
                "nem_carry_kwh": round(meter.nem_carry_kwh, 3),
            }
        )
        runtime.bill_attributes = {
            **bill.as_dict(),
            "afa_rate": self.config.afa_rate,
            "billing_start_day": account.billing_start_day,
            "history": meter.history,
        }
        self.publish_state(runtime)

    # -- Portal -----------------------------------------------------------------
    def sync_portal(self, runtime: AccountRuntime) -> None:
        """Fetch myTNB data, import statistics and update the portal sensors."""

        account = runtime.account
        session = runtime.portal
        if session is None:
            return
        now = datetime.now(MYT)
        window_start = datetime.combine(now.date() - timedelta(days=PORTAL_WINDOW_DAYS), datetime.min.time(), MYT)
        interval = self.config.portal_interval_hours * 3600
        _LOGGER.info("Syncing myTNB portal for '%s'", account.name)
        try:
            data = session.fetch(window_start, now + timedelta(days=1))
        except AuthError as err:
            self._portal_failed(runtime, STATUS_AUTH, str(err), retry_seconds=interval)
            return
        except MyTnbError as err:
            self._portal_failed(runtime, STATUS_ERROR, str(err), retry_seconds=min(interval, PORTAL_RETRY_SECONDS))
            return

        if account.statistics_enabled:
            self._import_statistics(account, window_start, data)
        else:
            _LOGGER.info("Energy dashboard import is switched off for '%s'", account.name)
        self._portal_succeeded(runtime, data, now, interval)

    def _import_statistics(self, account: Account, window_start: datetime, data: PortalData) -> None:
        try:
            stats.sync_statistics(
                self.supervisor.token,
                account_id=account.account_id,
                account_name=account.name,
                window_start=window_start,
                usage=data.usage,
                cost=data.cost,
            )
        except (stats.StatsError, OSError, ValueError) as err:
            _LOGGER.warning("Could not import statistics for '%s': %s", account.name, err)
        except Exception:  # websocket-client raises its own exception tree
            _LOGGER.exception("Could not import statistics for '%s'", account.name)

    def _portal_succeeded(self, runtime: AccountRuntime, data: PortalData, now: datetime, interval: int) -> None:
        account = runtime.account
        period_start = billing_period_start(now.date(), account.billing_start_day)
        cycle_usage = sum(p.value for p in data.usage if p.start.date() >= period_start)
        cycle_cost = sum(p.value for p in data.cost if p.start.date() >= period_start)
        latest_day = max((p.start.date() for p in data.usage), default=None)
        latest_usage = sum(p.value for p in data.usage if p.start.date() == latest_day) if latest_day else None
        latest_cost = sum(p.value for p in data.cost if p.start.date() == latest_day) if latest_day else None
        as_of = max((p.start for p in data.usage), default=None)

        runtime.values.update(
            {
                "period_start": cycle_start_iso(account, now),
                "portal_usage_cycle": round(cycle_usage, 3),
                "portal_cost_cycle": round(cycle_cost, 2),
                "portal_usage_latest_day": round(latest_usage, 3) if latest_usage is not None else None,
                "portal_cost_latest_day": round(latest_cost, 2) if latest_cost is not None else None,
                "portal_data_as_of": (as_of + timedelta(hours=1)).isoformat() if as_of else None,
                "portal_status": STATUS_OK,
                "portal_last_sync": now.isoformat(timespec="seconds"),
            }
        )
        runtime.portal_attributes = {"date": latest_day.isoformat() if latest_day else None}
        _LOGGER.info(
            "Portal '%s': %d usage / %d cost point(s); cycle %.2f kWh, RM %.2f",
            account.name,
            len(data.usage),
            len(data.cost),
            cycle_usage,
            cycle_cost,
        )
        if runtime.failure_notified:
            self.notify(account, "TNB portal sync recovered", f"myTNB data for '{account.name}' is syncing again.")
        runtime.portal_failures = 0
        runtime.failure_notified = False
        runtime.next_portal = time.monotonic() + interval
        self.publish_state(runtime)

    def _portal_failed(self, runtime: AccountRuntime, status: str, reason: str, retry_seconds: int) -> None:
        account = runtime.account
        runtime.portal_failures += 1
        runtime.values["portal_status"] = status
        runtime.next_portal = time.monotonic() + retry_seconds
        _LOGGER.error(
            "Portal sync for '%s' failed (%s): %s; retrying in %d min",
            account.name,
            status,
            reason,
            retry_seconds // 60,
        )
        self.publish_state(runtime)
        should_notify = status == STATUS_AUTH or runtime.portal_failures >= TRANSIENT_FAILURES_BEFORE_NOTIFY
        if should_notify and not runtime.failure_notified:
            runtime.failure_notified = True
            hint = "Check the myTNB email and password." if status == STATUS_AUTH else "The portal may be down."
            self.notify(account, "TNB portal sync failed", f"Could not fetch myTNB data for '{account.name}'. {hint}")

    # -- Loop -------------------------------------------------------------------
    def _tick(self) -> None:
        """One pass of the scheduler: options, discovery, holidays, then due accounts."""

        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        if time.monotonic() >= self.next_options_check:
            self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
            self.check_options()
        now = time.monotonic()
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

        today = datetime.now(MYT).date()
        if self.holiday_day != today:
            if self.holiday_day is None or self.holiday_day.year != today.year or today.day == 1:
                self.holidays.refresh(today.year)
            self.holiday_day = today
            self.publish_holiday()

        for runtime in self.runtimes:
            if self.stopping.is_set() or self.pending is not None:
                break  # apply a panel change before doing more slow work
            self._run_due(runtime)

    def _run_due(self, runtime: AccountRuntime) -> None:
        tick = time.monotonic()
        if runtime.meter is not None and runtime.account.meter_active and tick >= runtime.next_meter:
            runtime.next_meter = tick + self.config.meter_poll_seconds
            self.poll_meter(runtime)
        if runtime.portal is not None and runtime.account.portal_active and tick >= runtime.next_portal:
            self.sync_portal(runtime)
            # Small random gap so several accounts never hit the portal together.
            for other in self.runtimes:
                if other is not runtime and other.portal is not None:
                    other.next_portal = max(
                        other.next_portal,
                        time.monotonic() + random.uniform(5, PORTAL_STAGGER_SECONDS),  # noqa: S311 - jitter only
                    )

    def run(self) -> None:
        """Main scheduling loop."""

        server = PanelServer(WEB_DIR, lambda: panel.build_state(self), self.panel_toggle, self.panel_action)
        server.start()
        last_save = time.monotonic()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                self._tick()
                if time.monotonic() - last_save >= STORE_SAVE_SECONDS:
                    self.store.save()
                    last_save = time.monotonic()
                self.wake.wait(LOOP_SECONDS)
        finally:
            server.stop()
            self.store.save()
            if self.publisher is not None:
                self.publisher.stop()
            _LOGGER.info("Stopped")


def main() -> None:
    """Entry point."""

    merge_s6_environment()  # TZ first, so every log line uses local time
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    _LOGGER.info("Starting Power TNB %s", VERSION)
    try:
        raw_options = load_options()
        config = load_config(raw_options)
    except ConfigError:
        _LOGGER.exception("Invalid configuration")
        raise SystemExit(1) from None

    for account in config.accounts:
        sources = [
            name for name, on in (("myTNB portal", account.has_portal), ("energy sensor", account.has_meter)) if on
        ]
        _LOGGER.info(
            "Account '%s' (%s tariff, cycle starts day %d): %s",
            account.name,
            account.tariff,
            account.billing_start_day,
            " + ".join(sources),
        )

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

    app.migrate_overrides()
    app.run()


if __name__ == "__main__":
    main()
