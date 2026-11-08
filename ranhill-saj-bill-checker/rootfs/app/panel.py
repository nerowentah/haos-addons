"""Ranhill SAJ Bill Checker ingress panel: view model, toggles and actions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from config import Account, mask_number, slugify
from webui import PanelError, action, badge, notice, stat, toggle

if TYPE_CHECKING:
    from main import AccountRuntime, App

ACCOUNT_FIELDS = {"enabled"}
STATUS_BADGES = {
    "ok": badge("Checked", "ok"),
    "pending": badge("Waiting for first check"),
    "not_found": badge("Account not found", "error"),
    "layout_changed": badge("Site changed", "error"),
    "error": badge("Site error", "warn"),
}


def money(value: object) -> str | None:
    """``RM 1,234.56``; a credit shows as ``RM -12.30``."""

    return None if value is None else f"RM {float(value):,.2f}"  # type: ignore[arg-type]


def when(value: object) -> str | None:
    """Local short date/time of an ISO timestamp."""

    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value)).astimezone()
    except ValueError:
        return str(value)
    return moment.strftime("%d %b %H:%M")


def payment_text(due: object) -> str | None:
    """``Due`` / ``Paid`` for the payment-due flag (None while unknown)."""

    return None if due is None else ("Due" if due else "Paid")


def _account_card(runtime: AccountRuntime, account: Account) -> dict[str, object]:
    values = runtime.values
    status = str(values.get("status") or "pending")
    stats = [
        stat("Amount due", money(values.get("amount_due"))),
        stat("Payment", payment_text(values.get("payment_due"))),
        stat("Account no.", mask_number(account.account_number)),
        stat("Account holder", runtime.holder or None),
        stat("Last checked", when(values.get("last_checked"))),
        stat("Amount changed", when(values.get("amount_changed"))),
    ]
    card: dict[str, object] = {
        "title": account.name,
        "icon": "mdi:water",
        "subtitle": "Ranhill SAJ water account · Quick Payment lookup",
        "badge": badge("Paused", "off") if not account.enabled else STATUS_BADGES.get(status, badge(status)),
        "off": not account.enabled,
        "stats": stats,
        "toggles": [
            toggle(
                f"account:{account.account_id}:enabled",
                "Check this account",
                account.enabled,
                "Switch off to pause checks (entities show as unavailable)",
                icon="mdi:power",
            )
        ],
        "actions": [
            action(
                "check",
                "Check now",
                {"account": account.account_id},
                disabled=not account.enabled,
                reason="Switch the account on first" if not account.enabled else "",
                icon="mdi:refresh",
            )
        ],
    }
    if account.enabled and runtime.last_error and status != "ok":
        card["notice"] = notice(runtime.last_error, "warn" if status == "error" else "error")
    return card


def build_state(app: App) -> dict[str, object]:
    """The whole panel view model."""

    config = app.view_config()
    runtimes = {rt.account.account_id: rt for rt in app.runtimes}
    cards = [_account_card(runtimes[a.account_id], a) for a in config.accounts if a.account_id in runtimes]

    settings = {
        "title": "Settings",
        "icon": "mdi:cog",
        "subtitle": "Edit the remaining options in the app's Configuration tab",
        "stats": [
            stat("Check every", "week (or Check now)"),
            stat("Entities via", app.mqtt_status()),
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
                "Failure notifications",
                config.notify_on_failure,
                "Notify the phones below when checks keep failing, and again when they recover",
                icon="mdi:bell",
            ),
            toggle(
                "notify_on_unpaid",
                "Unpaid bill notifications",
                config.notify_on_unpaid,
                "Notify the phones below when a bill is due, weekly while it stays unpaid, and once when it is paid",
                icon="mdi:cash-clock",
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
                disabled=not (config.notify_on_failure or config.notify_on_unpaid),
                icon="mdi:cellphone",
            )
            for name in sorted(app.discovered)
        ],
        "empty": "No mobile apps found. Without phones a persistent notification is used.",
    }

    return {
        "title": "Ranhill SAJ Bill Checker",
        "subtitle": f"Version {app.version} · {len(config.accounts)} account(s)",
        "sections": [
            {"title": "Accounts", "icon": "mdi:water", "cards": cards},
            {"title": "Settings", "icon": "mdi:cog", "cards": [settings, phones_card]},
        ],
    }


def apply_toggle(options: dict[str, object], toggle_id: str, value: bool) -> None:
    """Write one toggle into a raw options dict (mutates ``options``)."""

    kind, _, rest = toggle_id.partition(":")
    if kind in ("notify_on_failure", "notify_on_unpaid", "mqtt") and not rest:
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
