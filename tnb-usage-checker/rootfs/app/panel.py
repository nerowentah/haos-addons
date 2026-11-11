"""TNB Usage Checker ingress panel: view model, toggles and actions."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import maintenance
import report
from config import Account, Config, mask_email, mask_number, slugify
from meter import MYT, billing_period_start, next_period_start
from stats import statistic_ids
from webui import PanelError, action, badge, chart, hero_item, link, notice, progress, row, stat, toggle

if TYPE_CHECKING:
    from main import AccountRuntime, App

ACCOUNT_FIELDS = {
    "enabled": "Account",
    "portal_enabled": "myTNB sync",
    "meter_enabled": "Live bill estimate",
    "statistics_enabled": "Energy dashboard import",
}
PORTAL_STATUS = {
    "ok": badge("Synced", "ok"),
    "pending": badge("Waiting for first sync"),
    "auth_error": badge("Login failed", "error"),
    "meter_error": badge("Smart meter not found", "error"),
    "error": badge("Portal error", "warn"),
    "maintenance": badge("TNB maintenance", "warn"),
}
STATS_STATUS = {
    "ok": "OK",
    "error": "Failed",
    "off": "Off",
    "pending": "Waiting for first sync",
}


def _money(value: object) -> str | None:
    return None if value is None else f"RM {float(value):,.2f}"  # type: ignore[arg-type]


def _kwh(value: object) -> str | None:
    return None if value is None else f"{float(value):,.2f}"  # type: ignore[arg-type]


def _when(value: object) -> str | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value)).astimezone(MYT)
    except ValueError:
        return str(value)
    return moment.strftime("%d %b %H:%M")


def _cycle_progress(account: Account, today: date) -> dict[str, object]:
    """'Day 16 of 30' through the current billing cycle."""

    start = billing_period_start(today, account.billing_start_day)
    end = next_period_start(start, account.billing_start_day)
    length = max((end - start).days, 1)
    day = min((today - start).days + 1, length)
    left = (end - today).days
    return progress(
        day / length,
        f"Day {day} of {length}",
        f"{left} day{'s' if left != 1 else ''} left · next cycle {end.day} {end:%b}",
    )


def _daily_chart(history: dict[date, float]) -> dict[str, object] | None:
    """Bar chart of the saved complete days, captioned with the newest day and its trend."""

    return chart(
        f"Daily usage · last {len(history)} day{'s' if len(history) != 1 else ''}",
        [(f"{day.day} {day:%b}", kwh) for day, kwh in history.items()],
        "kWh",
        report.latest_day_caption(history),
    )


def _day(value: object) -> str:
    """``23 Nov`` for an ISO date (the text itself when unreadable)."""

    try:
        return date.fromisoformat(str(value)).strftime("%d %b")
    except ValueError:
        return str(value)


def _statistics_stats(account: Account, values: dict[str, object]) -> list[dict[str, object]]:
    """Energy-dashboard import status tiles for a portal account."""

    if not account.statistics_enabled:
        return [stat("Energy import", "Off")]
    rows = [
        stat("Energy import", STATS_STATUS.get(str(values.get("stats_status")), "Not run yet")),
        stat("Last import", _when(values.get("stats_last_import"))),
    ]
    if values.get("stats_status") == "ok":
        rows.append(stat("Rows last import", values.get("stats_rows")))
    return rows


def _statistics_links(account: Account) -> list[dict[str, object]]:
    """The statistic ids to pick in the Energy dashboard, and a shortcut to it."""

    usage_id, cost_id = statistic_ids(account.account_id)
    description = (
        f"{usage_id} · {cost_id}"
        if account.statistics_enabled
        else "Import is off — switch on 'Energy dashboard import'"
    )
    return [
        row("Energy statistics", description, icon="mdi:chart-timeline-variant"),
        link(
            "Open Energy dashboard",
            "/config/energy",
            "Add the usage statistic as consumption and the cost statistic as its cost",
            icon="mdi:lightning-bolt",
        ),
    ]


def _statistics_notice(account: Account, values: dict[str, object]) -> dict[str, str] | None:
    """A highlighted error when the myTNB sync worked but the Energy import did not."""

    if not (account.has_portal and account.statistics_enabled and values.get("stats_status") == "error"):
        return None
    reason = str(values.get("stats_error") or "see the app log")
    return notice(f"Energy import failed: {reason}", "error")


def _account_card(
    runtime: AccountRuntime, account: Account, history: dict[date, float] | None = None, today: date | None = None
) -> dict[str, object]:
    values = dict(runtime.values)
    today = today or datetime.now(MYT).date()
    hero: list[dict[str, object]] = []
    stats: list[dict[str, object]] = []
    if account.has_meter:
        hero = [
            hero_item("Bill estimate", _money(values.get("bill_estimate"))),
            hero_item("Used this cycle", _kwh(values.get("cycle_import")), "kWh"),
        ]
        stats += [
            stat("Predicted bill", _money(values.get("predicted_bill"))),
            stat("Usage today", _kwh(values.get("today_import")), "kWh"),
        ]
        if account.tariff == "tou":
            stats += [
                stat(
                    "Peak / off-peak",
                    f"{_kwh(values.get('cycle_peak')) or '-'} / {_kwh(values.get('cycle_offpeak')) or '-'}",
                    "kWh",
                ),
                stat("Now", values.get("current_period")),
            ]
    if account.has_portal:
        if not hero:
            hero = [
                hero_item("myTNB cost this cycle", _money(values.get("portal_cost_cycle"))),
                hero_item("myTNB usage", _kwh(values.get("portal_usage_cycle")), "kWh"),
            ]
        else:
            stats += [
                stat("myTNB usage (cycle)", _kwh(values.get("portal_usage_cycle")), "kWh"),
                stat("myTNB cost (cycle)", _money(values.get("portal_cost_cycle"))),
            ]
        if values.get("amount_due") is not None:
            due = values.get("due_date")
            stats += [
                stat("Amount due", _money(values.get("amount_due"))),
                stat("Payment", ("Due" + (f" by {_day(due)}" if due else "")) if values.get("payment_due") else "Paid"),
            ]
        stats += [
            stat("myTNB data up to", _when(values.get("portal_data_as_of"))),
            stat("Last sync", _when(values.get("portal_last_sync"))),
            stat("Account no.", mask_number(account.ca_no)),
        ]
        stats += _statistics_stats(account, values)

    links = _statistics_links(account) if account.has_portal else []
    notice_item = _statistics_notice(account, values)

    toggles = [
        toggle(
            f"account:{account.account_id}:enabled",
            "Account",
            account.enabled,
            "Switch off to pause everything for this account",
            icon="mdi:power",
        )
    ]
    if account.has_portal:
        toggles.append(
            toggle(
                f"account:{account.account_id}:portal_enabled",
                "myTNB sync",
                account.portal_enabled,
                f"Log in as {mask_email(account.mytnb_username)} and fetch smart meter data",
                disabled=not account.enabled,
                icon="mdi:cloud-sync",
            )
        )
        toggles.append(
            toggle(
                f"account:{account.account_id}:statistics_enabled",
                "Energy dashboard import",
                account.statistics_enabled,
                "Import hourly usage and cost as long-term statistics",
                disabled=not (account.enabled and account.portal_enabled),
                icon="mdi:chart-bar",
            )
        )
    if account.has_meter:
        toggles.append(
            toggle(
                f"account:{account.account_id}:meter_enabled",
                "Live bill estimate",
                account.meter_enabled,
                f"Read {account.import_entity}" + (f" and {account.export_entity}" if account.export_entity else ""),
                disabled=not account.enabled,
                icon="mdi:gauge",
            )
        )

    if not account.enabled:
        status = badge("Paused", "off")
    elif account.has_portal and account.portal_enabled:
        status = PORTAL_STATUS.get(str(values.get("portal_status")), badge(str(values.get("portal_status"))))
    else:
        status = badge("Live", "ok")

    sources = " + ".join(
        name for name, on in (("myTNB", account.has_portal), ("energy sensor", account.has_meter)) if on
    )
    return {
        "id": f"account:{account.account_id}",
        "title": account.name,
        "icon": "mdi:home-lightning-bolt",
        "subtitle": f"{'Time-of-Use' if account.tariff == 'tou' else 'General'} tariff · cycle from day {account.billing_start_day} · {sources}",
        "badge": status,
        "off": not account.enabled,
        "hero": hero,
        "progress": _cycle_progress(account, today),
        "chart": _daily_chart(history) if account.has_portal and history else None,
        "stats": stats,
        "links": links,
        "notice": notice_item,
        "toggles": toggles,
        "toggles_label": "Settings",
        "actions": [
            action(
                "sync",
                "Sync myTNB now",
                {"account": account.account_id},
                disabled=not account.portal_active,
                icon="mdi:sync",
            )
        ]
        if account.has_portal
        else [],
    }


def _maintenance_text(config: Config) -> str:
    """'daily 00:00-06:00 (now, until 06:00)' or 'none'."""

    text = maintenance.describe(config.maintenance_windows)
    until = maintenance.window_end(config.maintenance_windows, datetime.now(MYT))
    return f"{text} (now, until {until:%H:%M})" if until else text


def build_state(app: App) -> dict[str, object]:
    """The whole panel view model."""

    config = app.view_config()
    runtimes = {rt.account.account_id: rt for rt in app.runtimes}
    cards = [
        _account_card(runtimes[a.account_id], a, app.daily_history(a.account_id))
        for a in config.accounts
        if a.account_id in runtimes
    ]

    today = datetime.now(MYT).date()
    afa_sen = config.afa_rate * 100
    settings = {
        "title": "Settings",
        "icon": "mdi:cog",
        "subtitle": "Edit the remaining options in the app's Configuration tab",
        "stats": [
            stat("AFA", f"{afa_sen:+.2f}", "sen/kWh"),
            stat("Off-peak holiday today", "Yes" if app.holidays.is_holiday(today) else "No"),
            stat("myTNB sync every", config.portal_interval_hours, "h"),
            stat("myTNB maintenance", _maintenance_text(config)),
            stat("Sensor poll every", config.meter_poll_seconds, "s"),
            stat("Entities via", app.mqtt_status()),
            stat(
                "myTNB logins",
                ", ".join(sorted({mask_email(a.mytnb_username) for a in config.accounts if a.has_portal})) or None,
            ),
        ],
        "toggles": [
            toggle(
                "mqtt",
                "Use MQTT when available",
                config.mqtt,
                "Publish devices via MQTT discovery when a broker (e.g. Mosquitto) is found; "
                "otherwise entities are created through the Home Assistant states API",
                icon="mdi:lan-connect",
            ),
            toggle(
                "notify_on_failure",
                "Sync failure notifications",
                config.notify_on_failure,
                "Notify the phones below when myTNB sync fails or recovers",
                icon="mdi:bell",
            ),
            toggle(
                "notify_on_unpaid",
                "Unpaid bill notifications",
                config.notify_on_unpaid,
                "Notify the phones below when myTNB shows a new amount due, weekly while it stays unpaid, "
                "and once when it is paid",
                icon="mdi:cash-clock",
            ),
            toggle(
                "daily_report",
                "Daily usage report",
                config.daily_report,
                "Notify the phones below once a day, when myTNB publishes a new complete day: "
                "that day's kWh and RM, the change from the day before, and the cycle so far",
                icon="mdi:chart-bar",
            ),
            toggle(
                "debug_login",
                "Verbose login log",
                config.debug_login,
                "Log every myTNB login step (redirects, status, cookie and form field names) to find login "
                "problems. Passwords, cookie values and account numbers are never logged",
                icon="mdi:script-text",
            ),
        ],
    }

    phones = app.phones
    phones_card = {
        "title": "Phones",
        "icon": "mdi:cellphone-message",
        "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
        "toggles": [
            toggle(
                f"phone:{name}",
                name.removeprefix("mobile_app_").replace("_", " "),
                phones.get(name, True) is not False,
                name,
                disabled=not (config.notify_on_failure or config.notify_on_unpaid or config.daily_report),
                icon="mdi:cellphone",
            )
            for name in sorted(app.discovered)
        ],
        "empty": "No mobile apps found. Without phones a persistent notification is used.",
    }

    return {
        "title": "TNB Usage Checker",
        "subtitle": f"Version {app.version} · {len(config.accounts)} account(s)",
        "sections": [
            {"title": "Accounts", "icon": "mdi:lightning-bolt", "cards": cards},
            {"title": "Settings", "icon": "mdi:cog", "cards": [settings, phones_card]},
        ],
    }


def apply_toggle(options: dict[str, object], toggle_id: str, value: bool) -> None:
    """Write one toggle into a raw options dict (mutates ``options``)."""

    kind, _, rest = toggle_id.partition(":")
    if kind in ("notify_on_failure", "notify_on_unpaid", "mqtt", "debug_login", "daily_report") and not rest:
        options[kind] = value
        return
    if kind == "account":
        account_id, _, field = rest.rpartition(":")
        if field in ACCOUNT_FIELDS:
            for row in options.get("accounts") or []:  # type: ignore[union-attr]
                if isinstance(row, dict) and slugify(str(row.get("name") or "")) == account_id:
                    row[field] = value
                    return
            raise PanelError("That account no longer exists; reload the page")
    raise PanelError("Unknown setting")
