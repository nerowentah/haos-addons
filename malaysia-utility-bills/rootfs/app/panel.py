"""Ingress panel view model: a section per provider, then the shared settings, phones and the move from the old apps."""

from __future__ import annotations

from typing import TYPE_CHECKING

from config import PROVIDERS
from core.webui import action, badge, notice, row, stat, toggle

if TYPE_CHECKING:
    from main import App


def namespace(card: dict[str, object], key: str) -> dict[str, object]:
    """Prefix a provider card's toggle, action and card ids with ``<key>:`` so the core can route them."""

    result = dict(card)
    if result.get("id"):
        result["id"] = f"{key}:{result['id']}"
    for field in ("toggles", "actions"):
        items = result.get(field)
        if isinstance(items, list):
            result[field] = [{**item, "id": f"{key}:{item['id']}"} for item in items if isinstance(item, dict)]
    return result


def _waiting_card(old_name: str) -> dict[str, object]:
    return {
        "title": f"Waiting for {old_name} to stop",
        "icon": "mdi:alert-circle",
        "badge": badge("Waiting", "warn"),
        "notice": notice(
            f"{old_name} is still running and writes the same entities. Stop it in Settings → Apps → "
            f"{old_name}; this app takes over within 5 minutes and keeps every entity and its history.",
            "warn",
        ),
    }


def _move_card(app: App) -> dict[str, object] | None:
    """Import buttons and status for each old app that is installed."""

    config = app.view_config()
    links: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    for spec in PROVIDERS:
        addon = app.old_apps.get(spec.key)
        if addon is None:
            continue
        running = addon.get("state") == "started"
        configured = spec.key in config.providers
        if configured and running:
            text = "Running: stop it so this app takes over its entities"
        elif configured:
            text = "Stopped: this app has taken over. Uninstall it when you're happy"
        else:
            text = f"Installed: import its accounts into {spec.prefix}accounts"
        links.append(row(spec.old_name, text, icon=spec.icon))
        if not configured:
            actions.append(
                action(
                    "import",
                    f"Import from {spec.old_name}",
                    {"provider": spec.key},
                    confirm=f"Copy the accounts and options of {spec.old_name} into this app?",
                    icon="mdi:restore",
                )
            )
    if not links:
        return None
    return {
        "title": "Moving from the old apps",
        "icon": "mdi:autorenew",
        "subtitle": "Entities keep their ids and history; only one app may run per provider",
        "links": links,
        "actions": actions,
    }


def build_state(app: App) -> dict[str, object]:
    """The whole panel view model."""

    config = app.view_config()
    sections: list[dict[str, object]] = []
    waiting = {spec.key for spec in app.blocked()}
    for spec in PROVIDERS:
        engine = app.engines.get(spec.key)
        provider_config = config.providers.get(spec.key)
        if engine is None or provider_config is None:
            continue
        cards = [namespace(card, spec.key) for card in engine.panel_cards(provider_config)]
        if spec.key in waiting:
            cards.insert(0, _waiting_card(spec.old_name))
        sections.append({"title": spec.section_title, "icon": spec.icon, "cards": cards})

    if not sections:
        sections.append(
            {
                "title": "Get started",
                "icon": "mdi:information",
                "cards": [
                    {
                        "title": "No accounts yet",
                        "icon": "mdi:information",
                        "empty": "Add accounts under "
                        + " or ".join(f"{spec.prefix}accounts" for spec in PROVIDERS)
                        + " in the app's Configuration tab, or import them from the old apps below.",
                    }
                ],
            }
        )

    settings = {
        "title": "Settings",
        "icon": "mdi:cog",
        "subtitle": "Edit the remaining options in the app's Configuration tab",
        "stats": [stat("Entities via", app.mqtt_status())],
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
                "Notify the phones below when a sync or check keeps failing, and again when it recovers",
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
    settings_cards: list[dict[str, object]] = [settings]
    for spec in PROVIDERS:
        engine = app.engines.get(spec.key)
        provider_config = config.providers.get(spec.key)
        if engine is not None and provider_config is not None:
            card = engine.panel_settings(provider_config)
            if card is not None:
                settings_cards.append(namespace(card, spec.key))

    phones = app.phones
    uses_phones = (
        config.notify_on_failure or config.notify_on_unpaid or any(e.uses_phones() for e in app.engines.values())
    )
    settings_cards.append(
        {
            "title": "Phones",
            "icon": "mdi:cellphone-message",
            "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
            "toggles": [
                toggle(
                    f"phone:{name}",
                    name.removeprefix("mobile_app_").replace("_", " "),
                    phones.get(name, True) is not False,
                    name,
                    disabled=not uses_phones,
                    icon="mdi:cellphone",
                )
                for name in sorted(app.discovered)
            ],
            "empty": "No mobile apps found. Without phones a persistent notification is used.",
        }
    )
    move = _move_card(app)
    if move is not None:
        settings_cards.append(move)
    sections.append({"title": "Settings", "icon": "mdi:cog", "cards": settings_cards})

    return {
        "title": "Malaysia Utility Bills",
        "subtitle": f"Version {app.version} · {config.account_count} account(s)",
        "sections": sections,
    }
