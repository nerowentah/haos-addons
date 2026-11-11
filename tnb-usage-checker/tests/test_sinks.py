"""Tests for the MQTT/states-API helpers and the automatic phone list."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import mqtt_pub
import supervisor


class FakeHttp(supervisor.Supervisor):
    """Supervisor whose HTTP layer returns canned replies or errors."""

    def __init__(self, reply: object = None, error: str | None = None) -> None:
        super().__init__("token")
        self.reply = reply
        self.error = error

    def _request(self, url: str, method: str = "GET", body: object | None = None) -> object:  # noqa: ARG002
        if self.error:
            raise supervisor.SupervisorError(self.error)
        return self.reply


class MqttServiceTest(unittest.TestCase):
    def test_no_broker_is_none(self) -> None:
        self.assertIsNone(FakeHttp(error="HTTP 400 from GET /services/mqtt").mqtt_service())

    def test_empty_reply_is_none(self) -> None:
        self.assertIsNone(FakeHttp(reply={"data": {}}).mqtt_service())

    def test_broker_returned(self) -> None:
        broker = {"host": "core-mosquitto", "port": 1883, "username": "u", "password": "p"}
        self.assertEqual(FakeHttp(reply={"data": broker}).mqtt_service(), broker)

    def test_unreachable_raises(self) -> None:
        with self.assertRaises(supervisor.SupervisorError):
            FakeHttp(error="GET /services/mqtt failed: timed out").mqtt_service()

    def test_discovery_failure_keeps_known_phones(self) -> None:
        self.assertIsNone(FakeHttp(error="HTTP 502 from GET /core/api/services").discover_mobile_targets())

    def test_discovery_lists_mobile_apps(self) -> None:
        services = [
            {"domain": "notify", "services": {"mobile_app_pixel": {}, "persistent_notification": {}}},
            {"domain": "light", "services": {"turn_on": {}}},
        ]
        self.assertEqual(FakeHttp(reply=services).discover_mobile_targets(), ["mobile_app_pixel"])


class PhoneListTest(unittest.TestCase):
    def test_new_phones_on_and_unlinked_dropped(self) -> None:
        merged = supervisor.merge_discovered(
            {"mobile_app_old": True, "mobile_app_a": False}, ["mobile_app_a", "mobile_app_b"]
        )
        self.assertEqual(merged, {"mobile_app_a": False, "mobile_app_b": True})

    def test_effective_targets(self) -> None:
        phones = {"mobile_app_a": False}
        self.assertEqual(supervisor.effective_targets(["mobile_app_a", "mobile_app_b"], phones), ["mobile_app_b"])

    def test_migration_reads_old_rows(self) -> None:
        raw = {"notification_overrides": [{"name": "mobile_app_a", "notify": False}, {"name": " "}, "junk"]}
        self.assertEqual(supervisor.overrides_from_options(raw), {"mobile_app_a": False})
        self.assertEqual(supervisor.overrides_from_options({}), {})


class StatesApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.account = config.build_account(1, {"name": "Home", "tariff": "general", "import_entity": "sensor.grid"})

    def test_entity_id_matches_mqtt_default(self) -> None:
        sensor = next(s for s in mqtt_pub.SENSORS if s.key == "bill_estimate")
        payload = mqtt_pub.sensor_config(self.account, sensor, "test")
        self.assertEqual(payload["default_entity_id"], mqtt_pub.entity_id("home", "bill_estimate"))

    def test_attributes_and_value(self) -> None:
        sensor = next(s for s in mqtt_pub.SENSORS if s.key == "bill_estimate")
        attributes = mqtt_pub.state_attributes(self.account, sensor)
        self.assertEqual(attributes["unit_of_measurement"], "MYR")
        self.assertEqual(attributes["friendly_name"], "TNB Home Bill estimate")
        self.assertNotIn("icon", attributes)
        self.assertEqual(mqtt_pub.state_value(sensor, 12.3456), "12.35")
        self.assertEqual(mqtt_pub.state_value(sensor, None), "unknown")

    def test_mqtt_option_defaults_on(self) -> None:
        raw = {"accounts": [{"name": "Home", "tariff": "general", "import_entity": "sensor.grid"}]}
        self.assertTrue(config.load_config(raw).mqtt)
        self.assertFalse(config.load_config({**raw, "mqtt": False}).mqtt)

    def test_statistics_import_diagnostics(self) -> None:
        status = next(s for s in mqtt_pub.SENSORS if s.key == "stats_status")
        last = next(s for s in mqtt_pub.SENSORS if s.key == "stats_last_import")

        self.assertEqual(status.source, mqtt_pub.SOURCE_PORTAL)
        self.assertTrue(status.diagnostic)
        self.assertTrue(last.diagnostic)
        self.assertEqual(last.device_class, "timestamp")
        self.assertEqual(mqtt_pub.entity_id("home", "stats_last_import"), "sensor.tnb_home_stats_last_import")


if __name__ == "__main__":
    unittest.main()
