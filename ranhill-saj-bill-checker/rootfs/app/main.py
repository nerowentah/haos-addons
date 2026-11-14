#!/usr/bin/env python3
"""Home Assistant app: Ranhill SAJ Bill Checker (Johor, Malaysia) - amount due for one or more water accounts."""

from __future__ import annotations

import copy
import logging
import os
import random
import signal
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Final, Literal, Protocol

import mqtt_pub
import panel
import redact
import unpaid
from config import (
    Account,
    Config,
    ConfigError,
    apply_log_level,
    load_config,
    load_options,
    mask_name,
    mask_number,
)
from ranhill import AccountNotFoundError, Bill, ParseError, SajClient, SajError
from safefile import atomic_write
from store import Store
from supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
    notification_id,
)
from webui import (
    PanelArgs,
    PanelError,
    PanelRouter,
    PanelServer,
    SecretError,
    SecretRefs,
    SetupView,
    initial_options,
    wait_for_valid_config,
)

_LOGGER = logging.getLogger("ranhill_saj")

VERSION = os.environ.get("RANHILL_SAJ_VERSION", "dev")
NOTIFICATION_PREFIX = "ranhill_saj_"
LOOP_SECONDS = 60  # longest idle sleep; the loop wakes earlier for due work or panel actions
MIN_SLEEP_SECONDS = 1
STORE_SAVE_SECONDS = 60
CHECK_INTERVAL_SECONDS = 7 * 24 * 3600  # bills are monthly: a weekly check (plus Check now) is plenty
RETRY_SECONDS = 30 * 60
PROBLEM_RETRY_SECONDS = 24 * 3600  # unknown account or changed page: retry daily, not weekly
STAGGER_SECONDS = 20
FIRST_CHECK_SECONDS = 5
TRANSIENT_FAILURES_BEFORE_NOTIFY = 3
OPTIONS_CHECK_SECONDS = 30
MQTT_CHECK_SECONDS = 60
PHONE_CHECK_SECONDS = 300
STATES_REFRESH_SECONDS = 300
WEB_DIR = Path(__file__).resolve().parent / "web"
UNRECOGNISED_PAGE = Path("/data/last_unrecognised_page.html")

type Status = Literal["ok", "pending", "not_found", "layout_changed", "error"]
STATUS_OK: Final = "ok"
STATUS_PENDING: Final = "pending"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_LAYOUT: Final = "layout_changed"
STATUS_ERROR: Final = "error"
STATUSES: tuple[Status, ...] = (STATUS_OK, STATUS_PENDING, STATUS_NOT_FOUND, STATUS_LAYOUT, STATUS_ERROR)
RETIRED_OPTIONS = ("poll_interval_hours",)


class BillSource(Protocol):
    """Looks up one account's bill (the Ranhill SAJ client, or a stand-in in the tests)."""

    def lookup(self, account_number: str, /) -> Bill:
        """The bill of ``account_number``; raises SajError."""
        ...


def now_iso() -> str:
    """The current local time as an ISO timestamp with offset."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _stamp(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(slots=True)
class AccountRuntime:
    """Live state for one account."""

    account: Account
    amount_due: float | None = None
    payment_due: bool | None = None
    amount_changed: str | None = None  # ISO timestamp
    status: Status = STATUS_PENDING
    last_checked: str | None = None  # ISO timestamp
    holder: str = ""  # masked
    next_check: float = 0.0
    failures: int = 0
    failure_notified: bool = False
    last_error: str = ""
    unpaid_notified_at: str = ""  # when the last unpaid-bill notice went out

    @property
    def values(self) -> dict[str, object]:
        """The state as published (MQTT state topic, states API) and saved."""

        return {
            "amount_due": self.amount_due,
            "payment_due": self.payment_due,
            "amount_changed": self.amount_changed,
            "status": self.status,
            "last_checked": self.last_checked,
        }

    def to_dict(self) -> dict[str, object]:
        """What survives a restart (no raw account number or name)."""

        return {**self.values, "holder": self.holder, "unpaid_notified_at": self.unpaid_notified_at or None}

    def restore(self, row: Mapping[str, object]) -> None:
        """Take over what ``to_dict`` saved; anything of the wrong type is left at its default."""

        amount = row.get("amount_due")
        if isinstance(amount, int | float) and not isinstance(amount, bool):
            self.amount_due = float(amount)
        due = row.get("payment_due")
        if isinstance(due, bool):
            self.payment_due = due
        self.amount_changed = _stamp(row.get("amount_changed"))
        self.last_checked = _stamp(row.get("last_checked"))
        saved_status = row.get("status")
        self.status = next((s for s in STATUSES if s == saved_status), STATUS_PENDING)
        self.holder = str(row.get("holder") or "")
        self.unpaid_notified_at = str(row.get("unpaid_notified_at") or "")

    def seconds_until_due(self) -> float | None:
        """Seconds left of the weekly interval after the last good check (None: check soon)."""

        checked = unpaid.parse_time(self.last_checked)
        if self.status != STATUS_OK or checked is None:
            return None
        elapsed = (datetime.now().astimezone() - checked).total_seconds()
        return CHECK_INTERVAL_SECONDS - elapsed if 0 <= elapsed < CHECK_INTERVAL_SECONDS else None


class App:
    """Scheduler that checks each account and publishes entities (MQTT or the states API)."""

    def __init__(
        self,
        config: Config,
        supervisor: Supervisor,
        store: Store,
        raw_options: dict[str, object] | None = None,
        client_factory: Callable[[], BillSource] = SajClient,
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}  # as stored: !secret references, never their values
        self.secrets = SecretRefs()
        self.version = VERSION
        self.supervisor = supervisor
        self.store = store
        self.client_factory = client_factory
        self.publisher: mqtt_pub.Publisher | None = None
        self.broker: mqtt_pub.Broker | None = None
        self.broker_found: bool | None = None  # None until the first check
        self.next_mqtt_check = 0.0
        self.next_states_refresh = 0.0
        self.next_phone_check = 0.0
        self.states_warned = False
        self.posted: dict[str, dict[str, object]] = {}  # last body posted per entity (states-API sink)
        self.republish = threading.Event()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None
        self.discovered: list[str] = []
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.runtimes: list[AccountRuntime] = []
        self._reconcile(config)

    def _reconcile(self, config: Config) -> None:
        """(Re)build account runtimes for ``config``, keeping the live state of unchanged accounts."""

        old = {rt.account.account_id: rt for rt in self.runtimes}
        saved = self.store.section("accounts")
        start = time.monotonic()
        runtimes: list[AccountRuntime] = []
        for index, account in enumerate(config.accounts):
            runtime = old.get(account.account_id)
            previous = runtime.account if runtime else None
            if runtime is None or previous is None or previous.account_number != account.account_number:
                runtime = self._new_runtime(account, saved, fresh=runtime is not None)
                soon = FIRST_CHECK_SECONDS + index * STAGGER_SECONDS
                runtime.next_check = start + max(soon, runtime.seconds_until_due() or 0.0)
            else:
                runtime.account = account
                if not previous.enabled and account.enabled:
                    runtime.next_check = start + FIRST_CHECK_SECONDS  # just switched on: check soon
            runtimes.append(runtime)
        for stale in set(saved) - {a.account_id for a in config.accounts}:
            saved.pop(stale, None)
            self.store.dirty = True
        self.runtimes = runtimes
        self.config = config

    def _new_runtime(self, account: Account, saved: dict[str, object], *, fresh: bool) -> AccountRuntime:
        runtime = AccountRuntime(account=account)
        row = saved.get(account.account_id)
        if not fresh and isinstance(row, dict) and row.get("number") == mask_number(account.account_number):
            runtime.restore(row)
        return runtime

    def _save_runtime(self, runtime: AccountRuntime) -> None:
        self.store.section("accounts")[runtime.account.account_id] = {
            **runtime.to_dict(),
            "number": mask_number(runtime.account.account_number),
        }
        self.store.dirty = True

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
        _LOGGER.info("Applied updated options (%d account(s))", len(config.accounts))

    def migrate_options(self) -> None:
        """Drop options this version no longer has (the check interval is fixed at a week now).

        Reads the stored options: /data/options.json is already filtered to the
        schema, so a retired key only shows up there.
        """

        try:
            stored = self.supervisor.fetch_options()
        except SupervisorError:
            return
        retired = [key for key in RETIRED_OPTIONS if key in stored]
        if not retired:
            return
        raw = {k: v for k, v in stored.items() if k not in retired}
        try:
            self.supervisor.write_options(raw)
        except SupervisorError:
            _LOGGER.warning("Could not remove %s from the options; it is ignored", ", ".join(retired))
            return
        self.raw_options = raw
        _LOGGER.info("Removed retired option(s) %s; accounts are now checked weekly", ", ".join(retired))

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel and apply them right away (any thread)."""

        with self.options_lock:
            try:
                base = self.supervisor.fetch_options()
            except SupervisorError as err:
                # Never fall back to the start-up copy: it holds resolved !secret values.
                raise PanelError("Home Assistant is not reachable; try again") from err
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(self.secrets.resolve_options(raw))
            except (ConfigError, SecretError) as err:
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
            config = load_config(self.secrets.resolve_options(raw))
        except (ConfigError, SecretError) as err:
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

    def panel_action(self, action_id: str, args: PanelArgs) -> str | None:
        """Handle a panel button."""

        if action_id == "check":
            account_id = args.ident("account")
            for runtime in self.runtimes:
                if runtime.account.account_id == account_id:
                    if not runtime.account.enabled:
                        raise PanelError("This account is switched off")
                    runtime.next_check = 0.0
                    self.wake.set()
                    return f"Checking {runtime.account.name}..."
            raise PanelError("That account no longer exists; reload the page")
        raise PanelError("Unknown action")

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
            targets = effective_targets(self.discovered, self.phones)
            _LOGGER.info("Notifications go to %d phone(s): %s", len(targets), ", ".join(targets) or "-")

    def set_phone(self, name: str, value: bool) -> None:
        """Panel switch for one phone (kept in /data, applies immediately)."""

        with self.options_lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            self.store.dirty = True

    def notify(self, account: Account, title: str, message: str, *, bill: bool = False) -> None:
        """Send a notification for an account when enabled (``bill``: an unpaid-bill notice)."""

        if self.config.notify_on_unpaid if bill else self.config.notify_on_failure:
            targets = effective_targets(self.discovered, self.phones)
            suffix = "_bill" if bill else ""
            self.supervisor.notify(
                targets, title, message, notification_id(NOTIFICATION_PREFIX, account.account_id) + suffix
            )

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
            service = self.supervisor.mqtt_service()
            broker = None if service is None else mqtt_pub.Broker.from_service(service)
        except (SupervisorError, ValueError) as err:
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
        if self.publisher is not None and broker == self.broker:
            return
        if self.publisher is not None:
            _LOGGER.info("MQTT broker settings changed; reconnecting")
            self.publisher.stop()
        else:
            _LOGGER.info("MQTT broker found; switching entities to MQTT discovery")
            self._delete_rest_entities()
        self.broker = broker
        self.publisher = mqtt_pub.Publisher(broker, self._request_republish)
        self.publisher.start()
        self.republish.set()

    def _request_republish(self) -> None:
        """Called from the MQTT thread (connect / Home Assistant online): republish on the main loop now."""

        self.republish.set()
        self.wake.set()

    def _retire_mqtt(self) -> None:
        """Remove retained discovery configs and disconnect."""

        publisher = self.publisher
        if publisher is None:
            return
        topics = self.store.data.get("discovery_topics")
        for topic in topics if isinstance(topics, list) else []:
            publisher.publish_json(str(topic), None)
        self.store.data["discovery_topics"] = []
        self.store.dirty = True
        publisher.stop()
        self.publisher = None
        self.broker = None

    @property
    def _use_states_api(self) -> bool:
        return self.publisher is None and (self.broker_found is not None or not self.config.mqtt)

    # -- Home Assistant states API (used while there is no MQTT) ------------------
    def _rest_entities(self) -> list[str]:
        value = self.store.data.get("rest_entities")
        return [str(v) for v in value] if isinstance(value, list) else []

    def _post_state(self, entity_id: str, state: str, attributes: dict[str, object]) -> None:
        body: dict[str, object] = {"state": state, "attributes": attributes}
        if self.posted.get(entity_id) == body:
            return  # unchanged: skip the HTTP round trip
        try:
            self.supervisor.core_post(f"/states/{entity_id}", body)
        except SupervisorError as err:
            if not self.states_warned:
                self.states_warned = True
                _LOGGER.warning("Could not update entities through the states API: %s", err)
            return
        self.states_warned = False
        self.posted[entity_id] = copy.deepcopy(body)

    def _post_states(self, runtime: AccountRuntime) -> None:
        account = runtime.account
        for sensor in mqtt_pub.SENSORS:
            attributes = mqtt_pub.state_attributes(account, sensor)
            if sensor.attributes:
                attributes.update(self._amount_attributes(runtime))
            value = mqtt_pub.state_value(sensor, runtime.values.get(sensor.key))
            self._post_state(
                mqtt_pub.entity_id(account.account_id, sensor.key, sensor.component),
                value if account.enabled else "unavailable",
                attributes,
            )

    def _sync_rest_entities(self) -> None:
        """Post every entity through the states API and drop ones that no longer exist."""

        current = [
            mqtt_pub.entity_id(rt.account.account_id, s.key, s.component)
            for rt in self.runtimes
            for s in mqtt_pub.SENSORS
        ]
        for stale in set(self._rest_entities()) - set(current):
            try:
                self.supervisor.core_delete(f"/states/{stale}")
            except SupervisorError:
                continue
        if self.store.data.get("rest_entities") != current:
            self.store.data["rest_entities"] = current
            self.store.dirty = True
        self.posted.clear()  # full re-post: states set through the API vanish when Home Assistant restarts
        for runtime in self.runtimes:
            self._post_states(runtime)
        self.next_states_refresh = time.monotonic() + STATES_REFRESH_SECONDS

    def _delete_rest_entities(self) -> None:
        """Remove states-API entities so MQTT discovery can own the same entity ids."""

        for entity_id in self._rest_entities():
            try:
                self.supervisor.core_delete(f"/states/{entity_id}")
            except SupervisorError as err:
                _LOGGER.warning("Could not remove %s: %s", entity_id, err)
        self.store.data["rest_entities"] = []
        self.posted.clear()
        self.store.dirty = True

    # -- Publishing -------------------------------------------------------------
    @staticmethod
    def _amount_attributes(runtime: AccountRuntime) -> dict[str, object]:
        return {"account_number": mask_number(runtime.account.account_number), "account_holder": runtime.holder or None}

    def publish_discovery(self) -> None:
        """Publish every entity: MQTT discovery when connected to a broker, else the states API."""

        if self.publisher is None:
            if self._use_states_api:
                self._sync_rest_entities()
            return
        published: list[str] = []
        for runtime in self.runtimes:
            for sensor in mqtt_pub.SENSORS:
                topic = mqtt_pub.discovery_topic(sensor.component, f"{runtime.account.account_id}_{sensor.key}")
                self.publisher.publish_json(topic, mqtt_pub.sensor_config(runtime.account, sensor, VERSION))
                published.append(topic)
        previous = self.store.data.get("discovery_topics")
        known = {str(topic) for topic in previous} if isinstance(previous, list) else set()
        for stale in known - set(published):
            self.publisher.publish_json(stale, None)
            _LOGGER.info("Removed a stale entity from MQTT discovery")
        self.store.data["discovery_topics"] = published
        self.store.dirty = True
        for runtime in self.runtimes:
            self.publish_state(runtime)

    def publish_state(self, runtime: AccountRuntime) -> None:
        """Publish an account's availability, state and attributes."""

        if self.publisher is None:
            if self._use_states_api:
                self._post_states(runtime)
            return
        account_id = runtime.account.account_id
        self.publisher.publish_raw(
            mqtt_pub.account_availability_topic(account_id), "online" if runtime.account.enabled else "offline"
        )
        self.publisher.publish_json(mqtt_pub.state_topic(account_id), runtime.values)
        self.publisher.publish_json(
            mqtt_pub.attributes_topic(account_id, "amount_due"), self._amount_attributes(runtime)
        )

    # -- Checks -----------------------------------------------------------------
    def check_account(self, runtime: AccountRuntime) -> None:
        """Look up the amount due of one account and update its sensors."""

        account = runtime.account
        interval = CHECK_INTERVAL_SECONDS
        _LOGGER.info("Checking Ranhill SAJ account '%s'", account.name)
        try:
            bill = self.client_factory().lookup(account.account_number)
        except AccountNotFoundError as err:
            self._failed(runtime, STATUS_NOT_FOUND, str(err), PROBLEM_RETRY_SECONDS)
            return
        except ParseError as err:
            self._save_unrecognised(err.page)
            self._failed(runtime, STATUS_LAYOUT, str(err), PROBLEM_RETRY_SECONDS)
            return
        except SajError as err:
            self._failed(runtime, STATUS_ERROR, str(err), min(interval, RETRY_SECONDS))
            return
        self._succeeded(runtime, bill, interval)

    def _succeeded(self, runtime: AccountRuntime, bill: Bill, interval: int) -> None:
        account = runtime.account
        checked = now_iso()
        previous = runtime.amount_due
        if previous is None or round(previous, 2) != bill.amount_due:
            runtime.amount_changed = checked
            if previous is not None:
                _LOGGER.info(
                    "Account '%s': amount due changed from RM %.2f to RM %.2f", account.name, previous, bill.amount_due
                )
        runtime.amount_due = bill.amount_due
        runtime.payment_due = unpaid.is_due(bill.amount_due)
        runtime.status = STATUS_OK
        runtime.last_checked = checked
        runtime.holder = mask_name(bill.holder) if bill.holder else ""
        _LOGGER.info("Account '%s': amount due RM %.2f", account.name, bill.amount_due)
        self._bill_notice(runtime, previous, bill.amount_due)
        if runtime.failure_notified:
            self.notify(
                account, "Ranhill SAJ check recovered", f"The water bill for '{account.name}' is checked again."
            )
        runtime.failures = 0
        runtime.failure_notified = False
        runtime.last_error = ""
        runtime.next_check = time.monotonic() + interval
        self._save_runtime(runtime)
        self.publish_state(runtime)

    def _bill_notice(self, runtime: AccountRuntime, previous: float | None, amount: float) -> None:
        """Tell the phones about a new or still-unpaid bill (and once when it is paid)."""

        now = datetime.now().astimezone()
        kind = unpaid.unpaid_notice(previous, amount, unpaid.parse_time(runtime.unpaid_notified_at), now)
        if kind is None:
            return
        _LOGGER.info("Account '%s': %s bill notice", runtime.account.name, kind)
        title, message = unpaid.notice_text(kind, "Water", runtime.account.name, amount)
        self.notify(runtime.account, title, message, bill=True)
        runtime.unpaid_notified_at = "" if kind == unpaid.PAID else now.isoformat(timespec="seconds")

    def _failed(self, runtime: AccountRuntime, status: Status, reason: str, retry_seconds: int) -> None:
        account = runtime.account
        runtime.failures += 1
        runtime.status = status
        runtime.last_checked = now_iso()
        runtime.last_error = reason
        runtime.next_check = time.monotonic() + retry_seconds
        _LOGGER.error(
            "Check of '%s' failed (%s): %s; retrying in %d min", account.name, status, reason, retry_seconds // 60
        )
        self._save_runtime(runtime)
        self.publish_state(runtime)
        should_notify = status != STATUS_ERROR or runtime.failures >= TRANSIENT_FAILURES_BEFORE_NOTIFY
        if should_notify and not runtime.failure_notified:
            runtime.failure_notified = True
            hints: dict[Status, str] = {
                STATUS_NOT_FOUND: "Check the account number in the app's Configuration tab.",
                STATUS_LAYOUT: "The Ranhill SAJ website may have changed; see the app log.",
            }
            hint = hints.get(status, "The Ranhill SAJ website may be down.")
            self.notify(
                account, "Ranhill SAJ check failed", f"Could not check the water bill for '{account.name}'. {hint}"
            )

    @staticmethod
    def _save_unrecognised(page: str) -> None:
        """Keep the page the parser did not understand in /data, so a layout change can be fixed."""

        if not page:
            return
        try:
            atomic_write(UNRECOGNISED_PAGE, page)
            _LOGGER.warning(
                "Saved the unrecognised page to %s (it may contain your name and address)", UNRECOGNISED_PAGE
            )
        except OSError:
            _LOGGER.debug("Could not save the unrecognised page", exc_info=True)

    # -- Loop -------------------------------------------------------------------
    def _tick(self) -> None:
        """One pass of the scheduler: options, MQTT, phones, then due accounts."""

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

        for runtime in self.runtimes:
            if self.stopping.is_set() or self.pending is not None:
                break  # apply a panel change before doing more slow work
            if runtime.account.enabled and time.monotonic() >= runtime.next_check:
                self.check_account(runtime)
                # Small random gap so several accounts never hit the site together.
                for other in self.runtimes:
                    if other is not runtime:
                        other.next_check = max(
                            other.next_check,
                            time.monotonic() + random.uniform(5, STAGGER_SECONDS),  # noqa: S311 - jitter only
                        )

    def _idle_seconds(self) -> float:
        """Sleep until the next scheduled job, at most LOOP_SECONDS."""

        deadlines = [self.next_options_check, self.next_mqtt_check, self.next_phone_check]
        if self.next_states_refresh:
            deadlines.append(self.next_states_refresh)
        deadlines += [rt.next_check for rt in self.runtimes if rt.account.enabled]
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
    "Open Settings → Apps → Ranhill SAJ Bill Checker → Configuration, fix the option named above and save. "
    "This page picks up the change within 15 seconds; no restart needed."
)
SETUP_EXAMPLE = """accounts:
  - name: Home
    account_number: "12345678-A1234567"
  - name: Shop
    account_number: "12345678-B7654321\""""


def _load_config(
    supervisor: Supervisor, secrets: SecretRefs, router: PanelRouter, stop: threading.Event
) -> tuple[dict[str, object], Config] | None:
    """The stored options (!secret references kept) and their config; waits in the panel while invalid."""

    raw_options = initial_options(load_options, supervisor.fetch_options, secrets)

    def validate(raw: dict[str, object]) -> Config:
        return load_config(secrets.resolve_options(raw))

    try:
        return raw_options, validate(raw_options)
    except (ConfigError, SecretError) as err:
        _LOGGER.error("Configuration problem: %s", err)  # noqa: TRY400 - no traceback needed
        _LOGGER.info("Waiting for a valid configuration; open the SAJ Water panel for details")
        view = SetupView("Ranhill SAJ Bill Checker", str(err), SETUP_HELP, SETUP_EXAMPLE)
        router.target = view
        result = wait_for_valid_config(supervisor.fetch_options, validate, view, stop)
        if result is None:
            return None
        raw, config = result
        if not isinstance(config, Config):
            raise TypeError("the options were validated into something that is not a Config") from None
        _LOGGER.info("Configuration is valid now; starting")
        return raw, config


def main() -> None:
    """Entry point."""

    merge_s6_environment()  # TZ first, so every log line uses local time
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    redact.install()  # registered secrets (the Supervisor token...) never reach the log
    _LOGGER.info("Starting Ranhill SAJ Bill Checker %s", VERSION)

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
        for account in config.accounts:
            _LOGGER.info("Account '%s': %s", account.name, mask_number(account.account_number))
        store = Store()
        store.load()
        app = App(config, supervisor, store, raw_options)
        app.secrets = secrets
        router.target = app
        if stop.is_set():
            return
        app.migrate_options()
        app.run()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
