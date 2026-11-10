"""Ranhill SAJ water (Johor): the amount due of each account, from the public Quick Payment page."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from core import unpaid
from core.supervisor import notification_id
from core.webui import PanelError

from providers.base import Engine as BaseEngine

from . import mqtt_pub, panel
from .config import Account, Config, mask_name, mask_number
from .ranhill import AccountNotFoundError, Bill, ParseError, SajClient, SajError

if TYPE_CHECKING:
    from core.scoped import ScopedStore

    from providers.base import Host

_LOGGER = logging.getLogger("ranhill_saj")

CHECK_INTERVAL_SECONDS = 7 * 24 * 3600  # bills are monthly: a weekly check (plus Check now) is plenty
RETRY_SECONDS = 30 * 60
PROBLEM_RETRY_SECONDS = 24 * 3600  # unknown account or changed page: retry daily, not weekly
STAGGER_SECONDS = 20
FIRST_CHECK_SECONDS = 5
TRANSIENT_FAILURES_BEFORE_NOTIFY = 3
UNRECOGNISED_PAGE = Path("/data/last_unrecognised_page.html")
NOTIFICATION_PREFIX = "ranhill_saj"  # kept from Ranhill SAJ Bill Checker

STATUS_OK = "ok"
STATUS_PENDING = "pending"
STATUS_NOT_FOUND = "not_found"
STATUS_LAYOUT = "layout_changed"
STATUS_ERROR = "error"
PERSISTED_KEYS = ("amount_due", "payment_due", "amount_changed", "status", "last_checked")


def now_iso() -> str:
    """The current local time as an ISO timestamp with offset."""

    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class AccountRuntime:
    """Live state for one account."""

    account: Account
    values: dict[str, object] = field(default_factory=dict)
    holder: str = ""  # masked
    next_check: float = 0.0
    failures: int = 0
    failure_notified: bool = False
    last_error: str = ""
    unpaid_notified_at: str = ""  # when the last unpaid-bill notice went out

    def to_dict(self) -> dict[str, object]:
        """What survives a restart (no raw account number or name)."""

        return {
            **{k: self.values.get(k) for k in PERSISTED_KEYS},
            "holder": self.holder,
            "unpaid_notified_at": self.unpaid_notified_at or None,
        }

    def seconds_until_due(self) -> float | None:
        """Seconds left of the weekly interval after the last good check (None: check soon)."""

        checked = unpaid.parse_time(self.values.get("last_checked"))
        if self.values.get("status") != STATUS_OK or checked is None:
            return None
        elapsed = (datetime.now().astimezone() - checked).total_seconds()
        return CHECK_INTERVAL_SECONDS - elapsed if 0 <= elapsed < CHECK_INTERVAL_SECONDS else None


class Engine(BaseEngine):
    """Checks each SAJ account weekly and publishes its entities."""

    key: ClassVar[str] = "saj"
    base_topic: ClassVar[str] = mqtt_pub.BASE_TOPIC
    entity_prefix: ClassVar[str] = "saj"

    def __init__(
        self,
        host: Host,
        config: Config,
        store: ScopedStore,
        client_factory: Callable[[], SajClient] = SajClient,
    ) -> None:
        super().__init__(host, config, store)
        self.config: Config = config
        self.client_factory = client_factory
        self.runtimes: list[AccountRuntime] = []
        self.reconcile(config)

    def reconcile(self, config: object) -> None:
        """(Re)build account runtimes for ``config``, keeping the live state of unchanged accounts."""

        assert isinstance(config, Config)  # noqa: S101 - the core passes this provider's config
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
        runtime = AccountRuntime(account=account, values=dict.fromkeys(PERSISTED_KEYS))
        runtime.values["status"] = STATUS_PENDING
        row = saved.get(account.account_id)
        if not fresh and isinstance(row, dict) and row.get("number") == mask_number(account.account_number):
            runtime.values.update({k: row.get(k) for k in PERSISTED_KEYS if row.get(k) is not None})
            runtime.holder = str(row.get("holder") or "")
            runtime.unpaid_notified_at = str(row.get("unpaid_notified_at") or "")
        return runtime

    def _save_runtime(self, runtime: AccountRuntime) -> None:
        self.store.section("accounts")[runtime.account.account_id] = {
            **runtime.to_dict(),
            "number": mask_number(runtime.account.account_number),
        }
        self.store.dirty = True

    def log_accounts(self) -> None:
        """One log line per account (masked number)."""

        for account in self.config.accounts:
            _LOGGER.info("SAJ account '%s': %s", account.name, mask_number(account.account_number))

    # -- Panel ------------------------------------------------------------------
    def panel_cards(self, config: object) -> list[dict[str, object]]:
        """One card per account (``config``: the options to show, maybe not applied yet)."""

        return panel.account_cards(self, config)  # type: ignore[arg-type]

    def panel_settings(self, config: object) -> dict[str, object] | None:  # noqa: ARG002 - nothing to configure
        """The fixed check schedule."""

        return panel.settings_card()

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Handle a panel button."""

        if action_id == "check":
            account_id = str(args.get("account") or "")
            for runtime in self.runtimes:
                if runtime.account.account_id == account_id:
                    if not runtime.account.enabled:
                        raise PanelError("This account is switched off")
                    runtime.next_check = 0.0
                    self.host.wake.set()
                    return f"Checking {runtime.account.name}..."
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
    @staticmethod
    def _amount_attributes(runtime: AccountRuntime) -> dict[str, object]:
        return {"account_number": mask_number(runtime.account.account_number), "account_holder": runtime.holder or None}

    def entity_ids(self) -> list[str]:
        """Every states-API entity of the SAJ accounts."""

        return [
            mqtt_pub.entity_id(rt.account.account_id, s.key, s.component)
            for rt in self.runtimes
            for s in mqtt_pub.SENSORS
        ]

    def discovery(self) -> list[tuple[str, dict[str, object]]]:
        """Discovery payloads of every account sensor."""

        return [
            (
                mqtt_pub.discovery_topic(sensor.component, f"{runtime.account.account_id}_{sensor.key}"),
                mqtt_pub.sensor_config(runtime.account, sensor, self.host.version),
            )
            for runtime in self.runtimes
            for sensor in mqtt_pub.SENSORS
        ]

    def publish_all(self) -> None:
        """State of every account."""

        for runtime in self.runtimes:
            self.publish_state(runtime)

    def _post_states(self, runtime: AccountRuntime) -> None:
        account = runtime.account
        for sensor in mqtt_pub.SENSORS:
            attributes = mqtt_pub.state_attributes(account, sensor)
            if sensor.attributes:
                attributes.update(self._amount_attributes(runtime))
            value = mqtt_pub.state_value(sensor, runtime.values.get(sensor.key))
            self.host.post_state(
                mqtt_pub.entity_id(account.account_id, sensor.key, sensor.component),
                value if account.enabled else "unavailable",
                attributes,
            )

    def publish_state(self, runtime: AccountRuntime) -> None:
        """Publish an account's availability, state and attributes."""

        publisher = self.host.publisher
        if publisher is None:
            if self.host.use_states_api():
                self._post_states(runtime)
            return
        account_id = runtime.account.account_id
        publisher.publish_raw(
            mqtt_pub.account_availability_topic(account_id), "online" if runtime.account.enabled else "offline"
        )
        publisher.publish_json(mqtt_pub.state_topic(account_id), runtime.values)
        publisher.publish_json(mqtt_pub.attributes_topic(account_id, "amount_due"), self._amount_attributes(runtime))

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
        previous = runtime.values.get("amount_due")
        if previous is None or round(float(previous), 2) != bill.amount_due:  # type: ignore[arg-type]
            runtime.values["amount_changed"] = checked
            if previous is not None:
                _LOGGER.info(
                    "Account '%s': amount due changed from RM %.2f to RM %.2f",
                    account.name,
                    float(previous),  # type: ignore[arg-type]
                    bill.amount_due,
                )
        runtime.values.update(
            {
                "amount_due": bill.amount_due,
                "payment_due": unpaid.is_due(bill.amount_due),
                "status": STATUS_OK,
                "last_checked": checked,
            }
        )
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

    def _bill_notice(self, runtime: AccountRuntime, previous: object, amount: float) -> None:
        """Tell the phones about a new or still-unpaid bill (and once when it is paid)."""

        now = datetime.now().astimezone()
        kind = unpaid.unpaid_notice(
            float(previous) if isinstance(previous, int | float) else None,
            amount,
            unpaid.parse_time(runtime.unpaid_notified_at),
            now,
        )
        if kind is None:
            return
        _LOGGER.info("Account '%s': %s bill notice", runtime.account.name, kind)
        title, message = unpaid.notice_text(kind, "Water", runtime.account.name, amount)
        self.notify(runtime.account, title, message, bill=True)
        runtime.unpaid_notified_at = "" if kind == unpaid.PAID else now.isoformat(timespec="seconds")

    def _failed(self, runtime: AccountRuntime, status: str, reason: str, retry_seconds: int) -> None:
        account = runtime.account
        runtime.failures += 1
        runtime.values["status"] = status
        runtime.values["last_checked"] = now_iso()
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
            hints = {
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
            UNRECOGNISED_PAGE.write_text(page, encoding="utf-8")
            _LOGGER.warning(
                "Saved the unrecognised page to %s (it may contain your name and address)", UNRECOGNISED_PAGE
            )
        except OSError:
            _LOGGER.debug("Could not save the unrecognised page", exc_info=True)

    # -- Loop -------------------------------------------------------------------
    def tick(self) -> None:
        """Check the accounts that are due."""

        for runtime in self.runtimes:
            if self.host.busy():
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

    def deadlines(self) -> list[float]:
        """Next check of every enabled account."""

        return [rt.next_check for rt in self.runtimes if rt.account.enabled]
