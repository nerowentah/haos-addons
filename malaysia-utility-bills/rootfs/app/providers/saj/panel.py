"""Ranhill SAJ cards for the ingress panel: accounts and their switches."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from core.webui import PanelError, action, badge, notice, stat, toggle

from .config import Account, Config, mask_number, slugify

if TYPE_CHECKING:
    from .engine import AccountRuntime, Engine

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


def account_cards(engine: Engine, config: Config) -> list[dict[str, object]]:
    """One card per configured account (``config``: the options to show, maybe not applied yet)."""

    runtimes = {rt.account.account_id: rt for rt in engine.runtimes}
    return [_account_card(runtimes[a.account_id], a) for a in config.accounts if a.account_id in runtimes]


def settings_card() -> dict[str, object]:
    """The fixed SAJ schedule (nothing else to set)."""

    return {
        "title": "Ranhill SAJ",
        "icon": "mdi:water",
        "subtitle": "Edit saj_accounts in the app's Configuration tab",
        "stats": [stat("Check every", "week (or Check now)")],
    }


def apply_toggle(options: dict[str, object], toggle_id: str, value: bool, prefix: str = "") -> None:
    """Write one SAJ toggle into a raw options dict (mutates ``options``; keys get ``prefix``, e.g. saj_)."""

    kind, _, rest = toggle_id.partition(":")
    if kind == "account":
        account_id, _, field = rest.rpartition(":")
        if field in ACCOUNT_FIELDS:
            for row in options.get(prefix + "accounts") or []:  # type: ignore[union-attr]
                if isinstance(row, dict) and slugify(str(row.get("name") or "")) == account_id:
                    row[field] = value
                    return
            raise PanelError("That account no longer exists; reload the page")
    raise PanelError("Unknown setting")
