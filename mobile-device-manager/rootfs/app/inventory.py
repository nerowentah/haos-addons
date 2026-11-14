"""Build the mobile-app device inventory from Home Assistant registry data (pure, testable)."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final, Literal

MOBILE_APP = "mobile_app"
type Flag = Literal["stale", "not_loaded", "duplicate", "no_notify"]
FLAG_STALE: Final = "stale"
FLAG_NOT_LOADED: Final = "not_loaded"
FLAG_DUPLICATE: Final = "duplicate"
FLAG_NO_NOTIFY: Final = "no_notify"
type LinkKind = Literal["automation", "script", "scene"]
LINK_KINDS: tuple[LinkKind, ...] = ("automation", "script", "scene")
type Reason = Literal["device", "notify"]  # a registry link, a call of the device's notify service
MIN_GROUP = 2


@dataclass(frozen=True, slots=True)
class Link:
    """Something that uses a device: an automation, script or scene."""

    kind: LinkKind
    entity_id: str
    name: str
    path: str | None
    reasons: frozenset[Reason] = frozenset()


@dataclass(slots=True)
class Device:
    """One Companion-app registration (links and flags are filled in after it is built)."""

    device_id: str
    entry_id: str
    name: str
    manufacturer: str = ""
    model: str = ""
    os_version: str = ""
    entry_state: str = ""
    notify_service: str | None = None
    last_seen: datetime | None = None
    battery: str | None = None
    entity_ids: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    flags: set[Flag] = field(default_factory=set)

    @property
    def removable(self) -> bool:
        """Removal is only offered when nothing uses the device (never forced)."""

        return not self.links


def slugify(text: str) -> str:
    """Home Assistant style slug (how mobile_app names its notify service)."""

    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def notify_services(services: object) -> set[str]:
    """``mobile_app_*`` notify service names from ``GET /api/services``."""

    names: set[str] = set()
    for entry in services if isinstance(services, list) else []:
        if isinstance(entry, dict) and entry.get("domain") == "notify":
            names.update(str(n) for n in entry.get("services") or {} if str(n).startswith(f"{MOBILE_APP}_"))
    return names


def build_devices(entries: object, devices: object, entities: object, states: object, services: object) -> list[Device]:
    """Join config entries, device/entity registries and states into Device records."""

    mobile_entries = {
        str(e["entry_id"]): e
        for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, dict) and e.get("domain", MOBILE_APP) == MOBILE_APP and e.get("entry_id")
    }
    by_device: dict[str, list[str]] = {}
    for ent in entities if isinstance(entities, list) else []:
        if isinstance(ent, dict) and ent.get("device_id") and ent.get("entity_id"):
            by_device.setdefault(str(ent["device_id"]), []).append(str(ent["entity_id"]))
    state_map = {
        str(s["entity_id"]): s
        for s in (states if isinstance(states, list) else [])
        if isinstance(s, dict) and "entity_id" in s
    }
    notify = notify_services(services)

    result: list[Device] = []
    for dev in devices if isinstance(devices, list) else []:
        if not isinstance(dev, dict):
            continue
        entry_ids = [str(e) for e in dev.get("config_entries") or [] if str(e) in mobile_entries]
        if not entry_ids:
            continue
        entry = mobile_entries[entry_ids[0]]
        name = str(dev.get("name_by_user") or dev.get("name") or entry.get("title") or "Unnamed device")
        entity_ids = sorted(by_device.get(str(dev.get("id")), []))
        device = Device(
            device_id=str(dev.get("id")),
            entry_id=entry_ids[0],
            name=name,
            manufacturer=str(dev.get("manufacturer") or ""),
            model=str(dev.get("model") or ""),
            os_version=str(dev.get("sw_version") or ""),
            entry_state=str(entry.get("state") or ""),
            entity_ids=entity_ids,
        )
        for candidate in (entry.get("title"), dev.get("name")):
            service = f"{MOBILE_APP}_{slugify(str(candidate or ''))}"
            if candidate and service in notify:
                device.notify_service = service
                break
        for entity_id in entity_ids:
            state = state_map.get(entity_id)
            if not state:
                continue
            seen = _parse_time(state.get("last_reported")) or _parse_time(state.get("last_updated"))
            if seen and (device.last_seen is None or seen > device.last_seen):
                device.last_seen = seen
            attrs = state.get("attributes") or {}
            if (
                device.battery is None
                and isinstance(attrs, dict)
                and attrs.get("device_class") == "battery"
                and attrs.get("unit_of_measurement") == "%"
            ):
                device.battery = f"{state.get('state')} %"
        result.append(device)
    return sorted(result, key=lambda d: d.name.lower())


def edit_path(kind: LinkKind, entity_id: str, state: Mapping[str, object] | None) -> str | None:
    """Frontend path that edits an automation/script/scene (None when it is not UI-editable)."""

    attrs = (state or {}).get("attributes") or {}
    item_id = attrs.get("id") if isinstance(attrs, dict) else None
    if kind == "script":
        return f"/config/script/edit/{entity_id.partition('.')[2]}"
    if kind in ("automation", "scene") and item_id:
        return f"/config/{kind}/edit/{item_id}"
    return None


def _friendly(entity_id: str, state: Mapping[str, object] | None) -> str:
    attrs = (state or {}).get("attributes") or {}
    name = attrs.get("friendly_name") if isinstance(attrs, dict) else None
    return str(name or entity_id)


def _link_kind(entity_id: str) -> LinkKind | None:
    domain = entity_id.partition(".")[0]
    for kind in LINK_KINDS:
        if kind == domain:
            return kind
    return None


def links_from_related(related: object, states: Mapping[str, Mapping[str, object]]) -> list[Link]:
    """Links from a ``search/related`` result (device or entity references)."""

    links: list[Link] = []
    if not isinstance(related, dict):
        return links
    for kind in LINK_KINDS:
        ids = related.get(kind)
        for raw_id in ids if isinstance(ids, list) else []:
            entity_id = str(raw_id)
            state = states.get(entity_id)
            links.append(
                Link(
                    kind,
                    entity_id,
                    _friendly(entity_id, state),
                    edit_path(kind, entity_id, state),
                    frozenset({"device"}),
                )
            )
    return links


def notify_references(service: str, configs: Mapping[str, object]) -> list[str]:
    """Entity ids of automations/scripts whose stored config calls ``notify.<service>``."""

    pattern = re.compile(rf"(?<![a-z0-9_]){re.escape(service)}(?![a-z0-9_])")
    return sorted(entity_id for entity_id, cfg in configs.items() if pattern.search(json.dumps(cfg)))


def merge_links(*groups: list[Link]) -> list[Link]:
    """De-duplicate links by entity id, keeping every reason."""

    merged: dict[str, Link] = {}
    for group in groups:
        for link in group:
            known = merged.get(link.entity_id)
            merged[link.entity_id] = link if known is None else replace(known, reasons=known.reasons | link.reasons)
    return sorted(merged.values(), key=lambda link: (LINK_KINDS.index(link.kind), link.name.lower()))


def notify_links(
    service: str | None, configs: Mapping[str, object], states: Mapping[str, Mapping[str, object]]
) -> list[Link]:
    """Links for automations/scripts that call the device's notify service."""

    if not service:
        return []
    links = []
    for entity_id in notify_references(service, configs):
        kind = _link_kind(entity_id)
        if kind is None:
            continue
        state = states.get(entity_id)
        links.append(
            Link(kind, entity_id, _friendly(entity_id, state), edit_path(kind, entity_id, state), frozenset({"notify"}))
        )
    return links


def apply_flags(devices: list[Device], now: datetime, stale_days: int, ignored: set[str]) -> None:
    """Set stale / not loaded / duplicate / no-notify flags in place."""

    cutoff = now - timedelta(days=stale_days)
    groups: dict[tuple[str, str], list[Device]] = {}
    for device in devices:
        device.flags = set()
        if device.entry_state and device.entry_state != "loaded":
            device.flags.add(FLAG_NOT_LOADED)
        if device.notify_service is None:
            device.flags.add(FLAG_NO_NOTIFY)
        if device.device_id not in ignored and (device.last_seen is None or device.last_seen < cutoff):
            device.flags.add(FLAG_STALE)
        groups.setdefault((device.name.strip().lower(), device.model.strip().lower()), []).append(device)
    epoch = datetime.min.replace(tzinfo=now.tzinfo)
    for group in groups.values():
        if len(group) < MIN_GROUP:
            continue
        newest = max(group, key=lambda d: d.last_seen or epoch)
        for device in group:
            if device is not newest:
                device.flags.add(FLAG_DUPLICATE)


def link_summary(links: list[Link]) -> str:
    """Human summary such as '2 automations and 1 script'."""

    parts = []
    for kind in LINK_KINDS:
        count = sum(1 for link in links if link.kind == kind)
        if count:
            parts.append(f"{count} {kind}{'' if count == 1 else 's'}")
    return " and ".join([", ".join(parts[:-1]), parts[-1]] if len(parts) > 1 else parts)
