"""TNB electricity: myTNB portal sync, Energy dashboard import and the live bill estimate."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from core import unpaid
from core.supervisor import SupervisorError, notification_id
from core.webui import PanelError

from providers.base import Engine as BaseEngine

from . import maintenance, mqtt_pub, panel, report, stats, tariff
from .config import Account, Config
from .holidays import HolidayCalendar
from .meter import MYT, AccountMeter, billing_period_start, is_peak, to_kwh
from .mytnb import (
    BillInfo,
    CredentialsError,
    MaintenanceError,
    MeterNotFoundError,
    MyTnbError,
    MyTnbSession,
    PortalData,
    set_debug,
)

if TYPE_CHECKING:
    from core.scoped import ScopedStore

    from providers.base import Host

_LOGGER = logging.getLogger("power_tnb")

PORTAL_WINDOW_DAYS = 30
PORTAL_RETRY_SECONDS = 30 * 60
CHART_DAYS = 14  # complete days kept for the panel's daily chart
METER_RETRY_SECONDS = 3 * 3600  # logged in but no meter: retry sooner than a bad password
PORTAL_STAGGER_SECONDS = 20
TRANSIENT_FAILURES_BEFORE_NOTIFY = 3
UNAVAILABLE_STATES = {"unknown", "unavailable", "none", ""}
NOTIFICATION_PREFIX = "power_tnb"  # kept from TNB Usage Checker

STATUS_OK = "ok"
STATUS_AUTH = "auth_error"
STATUS_ERROR = "error"
STATUS_METER = "meter_error"
STATUS_MAINTENANCE = "maintenance"
FAILURE_HINTS = {
    STATUS_AUTH: "Check the myTNB email and password.",
    STATUS_METER: "Login worked but no smart meter was found: paste the account's View Energy Usage link as smartmeter_url.",
}


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
    maintenance_backoff: maintenance.Backoff = field(default_factory=maintenance.Backoff)


class Engine(BaseEngine):
    """Polls meters, syncs the portal and publishes the TNB entities."""

    key: ClassVar[str] = "tnb"
    base_topic: ClassVar[str] = mqtt_pub.BASE_TOPIC
    entity_prefix: ClassVar[str] = "tnb"

    def __init__(self, host: Host, config: Config, store: ScopedStore) -> None:
        super().__init__(host, config, store)
        self.config: Config = config
        self.holidays = HolidayCalendar(config.extra_holidays, config.calendarific_api_key)
        self.holiday_day: date | None = None
        self.runtimes: list[AccountRuntime] = []
        self._apply(config)

    # -- Options ----------------------------------------------------------------
    def _apply(self, config: Config) -> None:
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

            creds = (account.mytnb_username, account.mytnb_password, account.ca_no, account.bill_no)
            old_creds = (
                (previous.mytnb_username, previous.mytnb_password, previous.ca_no, previous.bill_no)
                if previous
                else None
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
        set_debug(config.debug_login)

    def reconcile(self, config: object) -> None:
        """Switch to new TNB options in place (main thread only)."""

        assert isinstance(config, Config)  # noqa: S101 - the core passes this provider's config
        old = self.config
        self._apply(config)
        if (old.extra_holidays, old.calendarific_api_key) != (config.extra_holidays, config.calendarific_api_key):
            self.holidays = HolidayCalendar(config.extra_holidays, config.calendarific_api_key)
            self.holidays.refresh(datetime.now(MYT).year)
        if old.debug_login != config.debug_login:
            _LOGGER.info("Verbose myTNB login log %s", "on" if config.debug_login else "off")

    @staticmethod
    def _blank_values(account: Account) -> dict[str, object]:
        values: dict[str, object] = {sensor.key: None for sensor in mqtt_pub.sensors_for(account)}
        values["period_start"] = cycle_start_iso(account, datetime.now(MYT))
        if account.has_portal:
            values["portal_status"] = "pending"
        return values

    def log_accounts(self) -> None:
        """One log line per account."""

        for account in self.config.accounts:
            sources = [
                name for name, on in (("myTNB portal", account.has_portal), ("energy sensor", account.has_meter)) if on
            ]
            _LOGGER.info(
                "TNB account '%s' (%s tariff, cycle starts day %d): %s",
                account.name,
                account.tariff,
                account.billing_start_day,
                " + ".join(sources),
            )

    def uses_phones(self) -> bool:
        """The daily usage report also goes to the phones."""

        return self.config.daily_report

    # -- Panel ------------------------------------------------------------------
    def panel_cards(self, config: object) -> list[dict[str, object]]:
        """One card per account (``config``: the options to show, maybe not applied yet)."""

        return panel.account_cards(self, config)  # type: ignore[arg-type]

    def panel_settings(self, config: object) -> dict[str, object] | None:
        """TNB settings (AFA, holidays, sync interval, maintenance windows...)."""

        return panel.settings_card(self, config)  # type: ignore[arg-type]

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Handle a panel button."""

        if action_id == "sync":
            account_id = str(args.get("account") or "")
            for runtime in self.runtimes:
                if runtime.account.account_id == account_id:
                    if not runtime.account.portal_active:
                        raise PanelError("myTNB sync is switched off for this account")
                    until = self.maintenance_until()
                    if until is not None:
                        raise PanelError(
                            f"myTNB is in its maintenance window until {until:%H:%M}; it syncs by itself after that"
                        )
                    runtime.next_portal = 0.0
                    self.host.wake.set()
                    return f"Syncing myTNB for {runtime.account.name}..."
            raise PanelError("That account no longer exists; reload the page")
        raise PanelError("Unknown action")

    # -- Notifications ----------------------------------------------------------
    def notify(self, account: Account, title: str, message: str, *, bill: bool = False) -> None:
        """Send a notification for an account when enabled (``bill``: an unpaid-bill notice)."""

        self.host.notify(
            notification_id(account.account_id, NOTIFICATION_PREFIX) + ("_bill" if bill else ""),
            title,
            message,
            bill=bill,
        )

    # -- Entities ---------------------------------------------------------------
    def entity_ids(self) -> list[str]:
        """Every states-API entity of the TNB accounts, plus the holiday sensor."""

        current = [
            mqtt_pub.entity_id(rt.account.account_id, s.key, s.component)
            for rt in self.runtimes
            for s in mqtt_pub.sensors_for(rt.account)
        ]
        current.append(mqtt_pub.HOLIDAY_ENTITY_ID)
        return current

    def discovery(self) -> list[tuple[str, dict[str, object]]]:
        """Discovery payloads of every account sensor and the holiday sensor."""

        version = self.host.version
        payloads = [
            (
                mqtt_pub.discovery_topic(sensor.component, f"{runtime.account.account_id}_{sensor.key}"),
                mqtt_pub.sensor_config(runtime.account, sensor, version),
            )
            for runtime in self.runtimes
            for sensor in mqtt_pub.sensors_for(runtime.account)
        ]
        payloads.append((mqtt_pub.discovery_topic("binary_sensor", "holiday_today"), mqtt_pub.holiday_config(version)))
        return payloads

    def publish_all(self) -> None:
        """Availability and state of every account, and the holiday sensor."""

        for runtime in self.runtimes:
            self.publish_availability(runtime)
            self.publish_state(runtime)
        self.publish_holiday()

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
            self.host.post_state(
                mqtt_pub.entity_id(account.account_id, sensor.key, sensor.component), state, attributes
            )

    def _post_holiday(self) -> None:
        today = datetime.now(MYT).date()
        self.host.post_state(
            mqtt_pub.HOLIDAY_ENTITY_ID,
            "on" if self.holidays.is_holiday(today) else "off",
            {"friendly_name": mqtt_pub.HOLIDAY_NAME, "icon": "mdi:calendar-star"},
        )

    def publish_availability(self, runtime: AccountRuntime) -> None:
        """Mark each source online/offline according to the panel switches."""

        publisher = self.host.publisher
        if publisher is None:
            return
        account = runtime.account
        for source, configured, active in (
            (mqtt_pub.SOURCE_PORTAL, account.has_portal, account.portal_active),
            (mqtt_pub.SOURCE_METER, account.has_meter, account.meter_active),
        ):
            if configured:
                publisher.publish_raw(
                    mqtt_pub.source_availability_topic(account.account_id, source), "online" if active else "offline"
                )

    def publish_state(self, runtime: AccountRuntime) -> None:
        """Publish an account's state (and attributes) payloads."""

        publisher = self.host.publisher
        if publisher is None:
            if self.host.use_states_api():
                self._post_states(runtime)
            return
        account_id = runtime.account.account_id
        publisher.publish_json(mqtt_pub.state_topic(account_id), runtime.values)
        if runtime.account.has_meter:
            publisher.publish_json(mqtt_pub.attributes_topic(account_id, "bill_estimate"), runtime.bill_attributes)
        if runtime.account.has_portal:
            publisher.publish_json(
                mqtt_pub.attributes_topic(account_id, "portal_usage_latest_day"), runtime.portal_attributes
            )

    def publish_holiday(self) -> None:
        """Publish whether today is an off-peak holiday."""

        publisher = self.host.publisher
        if publisher is None:
            if self.host.use_states_api():
                self._post_holiday()
            return
        today = datetime.now(MYT).date()
        publisher.publish_raw(
            f"{mqtt_pub.BASE_TOPIC}/holiday_today", "ON" if self.holidays.is_holiday(today) else "OFF"
        )

    # -- Meter ------------------------------------------------------------------
    def _read_kwh(self, runtime: AccountRuntime, entity_id: str) -> float | None:
        try:
            state = self.host.supervisor.get_state(entity_id)
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
        if self._defer_for_maintenance_window(runtime, now):
            return
        window_start = datetime.combine(now.date() - timedelta(days=PORTAL_WINDOW_DAYS), datetime.min.time(), MYT)
        interval = self.config.portal_interval_hours * 3600
        _LOGGER.info("Syncing myTNB portal for '%s'", account.name)
        try:
            data = session.fetch(window_start, now + timedelta(days=1))
        except MyTnbError as err:
            self._portal_error(runtime, err, interval)
            return

        if account.statistics_enabled:
            self._import_statistics(account, window_start, data)
        else:
            _LOGGER.info("Energy dashboard import is switched off for '%s'", account.name)
        self._portal_succeeded(runtime, data, now, interval)

    def _portal_error(self, runtime: AccountRuntime, err: MyTnbError, interval: int) -> None:
        """Map a portal failure to a status and a retry delay."""

        retry = min(interval, PORTAL_RETRY_SECONDS)
        if isinstance(err, MaintenanceError):
            self._portal_in_maintenance(runtime, str(err))
        elif isinstance(err, CredentialsError):
            self._portal_failed(runtime, STATUS_AUTH, str(err), retry_seconds=interval)
        elif isinstance(err, MeterNotFoundError):
            self._portal_failed(runtime, STATUS_METER, str(err), retry_seconds=min(interval, METER_RETRY_SECONDS))
        else:  # incl. AuthError: smartliving session flakiness (redirect loops), not the password
            self._portal_failed(runtime, STATUS_ERROR, str(err), retry_seconds=retry)

    def maintenance_until(self, now: datetime | None = None) -> datetime | None:
        """End of the current myTNB maintenance window (Malaysia time), or None outside one."""

        return maintenance.window_end(self.config.maintenance_windows, now or datetime.now(MYT))

    def _defer_for_maintenance_window(self, runtime: AccountRuntime, now: datetime) -> bool:
        """Inside a maintenance window: don't call myTNB; try again shortly after it ends."""

        until = self.maintenance_until(now)
        if until is None:
            return False
        wait = (until - now).total_seconds() + maintenance.resume_jitter_seconds()
        runtime.next_portal = time.monotonic() + wait
        _LOGGER.info(
            "myTNB maintenance window (%s): '%s' syncs after %s",
            maintenance.describe(self.config.maintenance_windows),
            runtime.account.name,
            until.strftime("%a %H:%M"),
        )
        return True

    def _portal_in_maintenance(self, runtime: AccountRuntime, reason: str) -> None:
        """myTNB answered with a maintenance page: back off, keep the last data, no alert."""

        delay = runtime.maintenance_backoff.next_delay()
        runtime.next_portal = time.monotonic() + delay
        runtime.values["portal_status"] = STATUS_MAINTENANCE
        _LOGGER.warning("%s; '%s' retries in %d min", reason, runtime.account.name, delay // 60)
        self.publish_state(runtime)

    def _import_statistics(self, account: Account, window_start: datetime, data: PortalData) -> None:
        try:
            stats.sync_statistics(
                self.host.supervisor.token,
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
        if data.bill is not None:
            self._bill_succeeded(runtime, data.bill)
        _LOGGER.info(
            "Portal '%s': %d usage / %d cost point(s); cycle %.2f kWh, RM %.2f",
            account.name,
            len(data.usage),
            len(data.cost),
            cycle_usage,
            cycle_cost,
        )
        self._remember_daily_history(account, data)
        self._send_daily_report(account, data)
        if runtime.failure_notified:
            self.notify(account, "TNB portal sync recovered", f"myTNB data for '{account.name}' is syncing again.")
        runtime.portal_failures = 0
        runtime.failure_notified = False
        runtime.maintenance_backoff.reset()
        runtime.next_portal = time.monotonic() + interval
        self.publish_state(runtime)

    def _bill_succeeded(self, runtime: AccountRuntime, bill: BillInfo) -> None:
        """Publish the amount due and tell the phones about a new or still-unpaid bill."""

        account = runtime.account
        runtime.values.update(
            {
                "amount_due": round(bill.amount_due, 2),
                "payment_due": unpaid.is_due(bill.amount_due),
                "due_date": bill.due_date.isoformat() if bill.due_date else None,
            }
        )
        _LOGGER.info("Portal '%s': amount due RM %.2f", account.name, bill.amount_due)
        saved = self.store.section("bills")
        row = saved.get(account.account_id) if isinstance(saved.get(account.account_id), dict) else {}
        previous = row.get("amount_due")  # type: ignore[union-attr]
        now = datetime.now(MYT)
        kind = unpaid.unpaid_notice(
            float(previous) if isinstance(previous, int | float) else None,
            bill.amount_due,
            unpaid.parse_time(row.get("notified_at")),  # type: ignore[union-attr]
            now,
        )
        notified_at = row.get("notified_at")  # type: ignore[union-attr]
        if kind is not None:
            _LOGGER.info("Account '%s': %s bill notice", account.name, kind)
            title, message = unpaid.notice_text(kind, "TNB", account.name, bill.amount_due, bill.due_date)
            self.notify(account, title, message, bill=True)
            notified_at = None if kind == unpaid.PAID else now.isoformat(timespec="seconds")
        new_row = {"amount_due": round(bill.amount_due, 2), "notified_at": notified_at}
        if row != new_row:
            saved[account.account_id] = new_row
            self.store.dirty = True

    def _remember_daily_history(self, account: Account, data: PortalData) -> None:
        """Keep the last complete days (kWh) in /data for the panel's daily chart."""

        days = report.complete_days(data.usage)
        recent = {day.isoformat(): round(kwh, 3) for day, kwh in sorted(days.items())[-CHART_DAYS:]}
        history = self.store.section("daily_history")
        if recent and history.get(account.account_id) != recent:
            history[account.account_id] = recent
            self.store.dirty = True

    def daily_history(self, account_id: str) -> dict[date, float]:
        """The saved complete days for an account, oldest first (for the panel)."""

        saved = self.store.section("daily_history").get(account_id)
        result: dict[date, float] = {}
        for key, value in saved.items() if isinstance(saved, dict) else []:
            try:
                result[date.fromisoformat(str(key))] = float(value)
            except (TypeError, ValueError):
                continue
        return dict(sorted(result.items()))

    def _send_daily_report(self, account: Account, data: PortalData) -> None:
        """Notify once per account for each newly complete day of portal data."""

        if not self.config.daily_report:
            return
        sent = self.store.section("daily_reports")
        try:
            last = date.fromisoformat(str(sent.get(account.account_id))) if sent.get(account.account_id) else None
        except ValueError:
            last = None
        daily = report.build_report(
            data.usage, data.cost, lambda day: billing_period_start(day, account.billing_start_day), last
        )
        if daily is None:
            return
        self.host.supervisor.notify(
            self.host.notify_targets(),
            daily.title,
            daily.message(account.name),
            notification_id(account.account_id, NOTIFICATION_PREFIX) + "_daily",
        )
        _LOGGER.info("Daily report for '%s' (%s): %.2f kWh", account.name, daily.day.isoformat(), daily.usage_kwh)
        sent[account.account_id] = daily.day.isoformat()
        self.store.dirty = True

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
        should_notify = status != STATUS_ERROR or runtime.portal_failures >= TRANSIENT_FAILURES_BEFORE_NOTIFY
        if should_notify and not runtime.failure_notified:
            runtime.failure_notified = True
            hint = FAILURE_HINTS.get(status, "The portal may be down.")
            self.notify(account, "TNB portal sync failed", f"Could not fetch myTNB data for '{account.name}'. {hint}")

    # -- Loop -------------------------------------------------------------------
    def tick(self) -> None:
        """Holidays, then due meter polls and portal syncs."""

        today = datetime.now(MYT).date()
        if self.holiday_day != today:
            if self.holiday_day is None or self.holiday_day.year != today.year or today.day == 1:
                self.holidays.refresh(today.year)
            self.holiday_day = today
            self.publish_holiday()

        for runtime in self.runtimes:
            if self.host.busy():
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

    def deadlines(self) -> list[float]:
        """Next meter polls and portal syncs."""

        result: list[float] = []
        for runtime in self.runtimes:
            if runtime.meter is not None and runtime.account.meter_active:
                result.append(runtime.next_meter)
            if runtime.portal is not None and runtime.account.portal_active:
                result.append(runtime.next_portal)
        return result
