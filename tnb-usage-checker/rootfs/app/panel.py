"""TNB Usage Checker ingress panel: view model, toggles and actions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from config import Account, mask_email, mask_number, slugify
from meter import MYT
from webui import PanelError, action, badge, stat, toggle

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


def _account_card(runtime: AccountRuntime, account: Account) -> dict[str, object]:
    values = dict(runtime.values)
    stats: list[dict[str, object]] = []
    if account.has_meter:
        stats += [
            stat("Bill estimate", _money(values.get("bill_estimate"))),
            stat("Predicted bill", _money(values.get("predicted_bill"))),
            stat("Usage this cycle", _kwh(values.get("cycle_import")), "kWh"),
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
        stats.append(stat("Days left in cycle", values.get("days_remaining")))
    if account.has_portal:
        stats += [
            stat("Account no.", mask_number(account.ca_no)),
            stat("myTNB usage (cycle)", _kwh(values.get("portal_usage_cycle")), "kWh"),
            stat("myTNB cost (cycle)", _money(values.get("portal_cost_cycle"))),
            stat("myTNB data up to", _when(values.get("portal_data_as_of"))),
            stat("Last sync", _when(values.get("portal_last_sync"))),
        ]

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
        "title": account.name,
        "icon": "mdi:home-lightning-bolt",
        "subtitle": f"{'Time-of-Use' if account.tariff == 'tou' else 'General'} tariff · cycle from day {account.billing_start_day} · {sources}",
        "badge": status,
        "off": not account.enabled,
        "stats": stats,
        "toggles": toggles,
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


def build_state(app: App) -> dict[str, object]:
    """The whole panel view model."""

    config = app.view_config()
    runtimes = {rt.account.account_id: rt for rt in app.runtimes}
    cards = [_account_card(runtimes[a.account_id], a) for a in config.accounts if a.account_id in runtimes]

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
                disabled=not config.notify_on_failure,
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
    if kind in ("notify_on_failure", "mqtt", "debug_login", "daily_report") and not rest:
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
