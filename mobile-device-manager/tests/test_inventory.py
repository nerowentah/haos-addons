"""Tests for the mobile-app inventory, links and flags."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import inventory

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


ENTRIES = [
    {"entry_id": "e1", "domain": "mobile_app", "title": "Alex's Phone", "state": "loaded"},
    {"entry_id": "e2", "domain": "mobile_app", "title": "Tablet X1", "state": "loaded"},
    {"entry_id": "e3", "domain": "mobile_app", "title": "Tablet X1", "state": "setup_error"},
]
DEVICES = [
    {
        "id": "d1",
        "config_entries": ["e1"],
        "name": "Alex's Phone",
        "manufacturer": "ExampleCo",
        "model": "Phone-1",
        "sw_version": "26.1",
    },
    {
        "id": "d2",
        "config_entries": ["e2"],
        "name": "Tablet X1",
        "name_by_user": None,
        "manufacturer": "ExampleCo",
        "model": "Tablet X1",
    },
    {"id": "d3", "config_entries": ["e3"], "name": "Tablet X1", "manufacturer": "ExampleCo", "model": "Tablet X1"},
    {"id": "dx", "config_entries": ["other"], "name": "Hue bridge"},
]
ENTITIES = [
    {"entity_id": "sensor.alex_phone_battery_level", "device_id": "d1"},
    {"entity_id": "device_tracker.alex_phone", "device_id": "d1"},
    {"entity_id": "sensor.tablet_x1_battery_level", "device_id": "d2"},
    {"entity_id": "sensor.tablet_x1_battery_level_2", "device_id": "d3"},
]
STATES = [
    {
        "entity_id": "sensor.alex_phone_battery_level",
        "state": "80",
        "last_updated": iso(0.1),
        "last_reported": iso(0.01),
        "attributes": {"device_class": "battery", "unit_of_measurement": "%"},
    },
    {"entity_id": "device_tracker.alex_phone", "state": "home", "last_updated": iso(2), "attributes": {}},
    {"entity_id": "sensor.tablet_x1_battery_level", "state": "55", "last_updated": iso(1), "attributes": {}},
    {"entity_id": "sensor.tablet_x1_battery_level_2", "state": "10", "last_updated": iso(90), "attributes": {}},
    {"entity_id": "automation.leave_home", "state": "on", "attributes": {"id": "1700", "friendly_name": "Leave home"}},
    {"entity_id": "automation.yaml_only", "state": "on", "attributes": {"friendly_name": "YAML only"}},
    {"entity_id": "script.ping_phone", "state": "off", "attributes": {"friendly_name": "Ping phone"}},
]
SERVICES = [
    {
        "domain": "notify",
        "services": {"mobile_app_alex_s_phone": {}, "mobile_app_tablet_x1": {}, "notify": {}},
    }
]


def build() -> list[inventory.Device]:
    return inventory.build_devices(ENTRIES, DEVICES, ENTITIES, STATES, SERVICES)


class BuildTest(unittest.TestCase):
    def test_only_mobile_app_devices(self) -> None:
        self.assertEqual([d.device_id for d in build()], ["d1", "d2", "d3"])

    def test_last_seen_prefers_newest_report(self) -> None:
        alex = build()[0]
        self.assertEqual(alex.last_seen, NOW - timedelta(days=0.01))
        self.assertEqual(alex.battery, "80 %")
        self.assertEqual(alex.os_version, "26.1")

    def test_notify_service_matched_by_slug(self) -> None:
        alex, phone, old = build()
        self.assertEqual(alex.notify_service, "mobile_app_alex_s_phone")
        self.assertEqual(phone.notify_service, "mobile_app_tablet_x1")
        self.assertEqual(old.notify_service, "mobile_app_tablet_x1")  # same title, same service name

    def test_slugify(self) -> None:
        self.assertEqual(inventory.slugify("Alex's Phone"), "alex_s_phone")
        self.assertEqual(inventory.slugify("Pixel 9 Pró"), "pixel_9_pro")


class FlagTest(unittest.TestCase):
    def test_flags(self) -> None:
        alex, phone, old = build()
        inventory.apply_flags([alex, phone, old], NOW, 30, set())
        self.assertEqual(alex.flags, set())
        self.assertEqual(phone.flags, set())
        self.assertEqual(old.flags, {inventory.FLAG_STALE, inventory.FLAG_NOT_LOADED, inventory.FLAG_DUPLICATE})

    def test_ignored_never_stale(self) -> None:
        devices = build()
        inventory.apply_flags(devices, NOW, 30, {"d3"})
        self.assertNotIn(inventory.FLAG_STALE, devices[2].flags)

    def test_no_notify_service(self) -> None:
        devices = inventory.build_devices(ENTRIES, DEVICES, ENTITIES, STATES, [])
        inventory.apply_flags(devices, NOW, 30, set())
        self.assertTrue(all(inventory.FLAG_NO_NOTIFY in d.flags for d in devices))


class LinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.states = {s["entity_id"]: s for s in STATES}

    def test_related_links_with_edit_paths(self) -> None:
        related = {
            "automation": ["automation.leave_home", "automation.yaml_only"],
            "script": ["script.ping_phone"],
            "device": ["d1"],
        }
        links = inventory.links_from_related(related, self.states)
        paths = {link.entity_id: link.path for link in links}
        self.assertEqual(paths["automation.leave_home"], "/config/automation/edit/1700")
        self.assertIsNone(paths["automation.yaml_only"])
        self.assertEqual(paths["script.ping_phone"], "/config/script/edit/ping_phone")
        self.assertEqual(links[0].name, "Leave home")

    def test_notify_references_respect_word_boundaries(self) -> None:
        configs = {
            "automation.leave_home": {"actions": [{"action": "notify.mobile_app_tablet_x1", "data": {"message": "x"}}]},
            "script.ping_phone": {"sequence": [{"service": "notify.mobile_app_tablet_x1_2"}]},
        }
        self.assertEqual(inventory.notify_references("mobile_app_tablet_x1", configs), ["automation.leave_home"])

    def test_merge_keeps_both_reasons(self) -> None:
        related = inventory.links_from_related({"automation": ["automation.leave_home"]}, self.states)
        configs = {"automation.leave_home": {"actions": [{"action": "notify.mobile_app_tablet_x1"}]}}
        notify = inventory.notify_links("mobile_app_tablet_x1", configs, self.states)
        merged = inventory.merge_links(related, notify)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].reasons, {"device", "notify"})

    def test_remove_guard_and_summary(self) -> None:
        device = build()[1]
        self.assertTrue(device.removable)
        device.links = inventory.links_from_related(
            {"automation": ["automation.leave_home", "automation.yaml_only"], "script": ["script.ping_phone"]},
            self.states,
        )
        self.assertFalse(device.removable)
        self.assertEqual(inventory.link_summary(device.links), "2 automations and 1 script")


class ConfigTest(unittest.TestCase):
    def test_defaults_and_clamping(self) -> None:
        cfg = config.load_config({"scan_minutes": 1, "stale_days": 999, "ignored_devices": ["b", " a ", "", "a"]})
        self.assertEqual((cfg.scan_minutes, cfg.stale_days), (5, 365))
        self.assertEqual(cfg.ignored_devices, ["a", "b"])
        self.assertTrue(cfg.notify_on_attention)


if __name__ == "__main__":
    unittest.main()
