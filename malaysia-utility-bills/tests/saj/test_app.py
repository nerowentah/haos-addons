"""Tests for options validation, masking, panel toggles and the check scheduler (no network)."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rootfs" / "app"))

import panel as app_panel
from core import store, supervisor, unpaid, webui
from providers.saj import config, mqtt_pub, panel, ranhill
from providers.saj import engine as saj_engine

from tests import helpers


def options(*accounts: dict[str, object], **extra: object) -> dict[str, object]:
    return {"notify_on_failure": True, "mqtt": True, "accounts": list(accounts), **extra}


class ConfigTest(unittest.TestCase):
    def test_hyphens_spaces_and_case_are_normalised(self) -> None:
        cfg = config.load_config(options({"name": "Home", "account_number": " 12345678-a 1234567 "}))
        self.assertEqual(cfg.accounts[0].account_number, "12345678A1234567")
        self.assertEqual(cfg.accounts[0].account_id, "home")

    def test_empty_number_names_the_format(self) -> None:
        with self.assertRaisesRegex(config.ConfigError, "16-character"):
            config.load_config(options({"name": "Home", "account_number": ""}))

    def test_wrong_shape_is_rejected(self) -> None:
        for bad in ("1234567812345678", "12345678A123456", "1234567AA1234567"):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, "8 digits, a letter"):
                config.load_config(options({"name": "Home", "account_number": bad}))

    def test_duplicates_are_rejected(self) -> None:
        with self.assertRaisesRegex(config.ConfigError, "distinct names"):
            config.load_config(
                options(
                    {"name": "Home", "account_number": "12345678A1234567"},
                    {"name": "home", "account_number": "12345678B1234567"},
                )
            )
        with self.assertRaisesRegex(config.ConfigError, "repeats an account number"):
            config.load_config(
                options(
                    {"name": "Home", "account_number": "12345678A1234567"},
                    {"name": "Shop", "account_number": "12345678-A1234567"},
                )
            )

    def test_no_accounts(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_config(options())

    def test_defaults_and_log_level(self) -> None:
        home = {"name": "Home", "account_number": "12345678A1234567"}
        cfg = config.load_config(options(home))
        self.assertTrue(cfg.notify_on_unpaid)
        self.assertEqual(cfg.log_level, "info")
        self.assertEqual(config.load_config(options(home, log_level="DEBUG")).log_level, "debug")
        self.assertEqual(config.load_config(options(home, log_level="loud")).log_level, "info")
        self.assertFalse(config.load_config(options(home, notify_on_unpaid=False)).notify_on_unpaid)

    def test_retired_interval_is_ignored(self) -> None:
        cfg = config.load_config(options({"name": "Home", "account_number": "12345678A1234567"}, poll_interval_hours=6))
        self.assertFalse(hasattr(cfg, "poll_interval_hours"))

    def test_setup_view_waits_for_a_valid_config(self) -> None:
        view = webui.SetupView("Ranhill SAJ Bill Checker", "bad", "fix it")
        good = options({"name": "Home", "account_number": "12345678A1234567"})
        stop = threading.Event()
        result = webui.wait_for_valid_config(lambda: good, config.load_config, view, stop, interval=0.01)
        self.assertIsNotNone(result)

    def test_masking(self) -> None:
        self.assertEqual(config.mask_number("12345678A1234567"), "1234••••4567")
        self.assertEqual(config.mask_name("AHMAD BIN ALI"), "A•••• B•••• A••••")
        self.assertEqual(config.mask_name(""), "-")


class PanelToggleTest(unittest.TestCase):
    def test_account_toggle(self) -> None:
        raw = options({"name": "Home", "account_number": "12345678A1234567"})
        panel.apply_toggle(raw, "account:home:enabled", False)
        self.assertIs(raw["accounts"][0]["enabled"], False)  # type: ignore[index]
        prefixed = helpers.merged("saj", options({"name": "Home", "account_number": "12345678A1234567"}))
        panel.apply_toggle(prefixed, "account:home:enabled", False, "saj_")
        self.assertIs(prefixed["saj_accounts"][0]["enabled"], False)  # type: ignore[index]
        with self.assertRaises(webui.PanelError):
            panel.apply_toggle(raw, "mqtt", False)  # shared switches belong to the app core

    def test_unknown_toggles(self) -> None:
        raw = options({"name": "Home", "account_number": "12345678A1234567"})
        with self.assertRaises(webui.PanelError):
            panel.apply_toggle(raw, "account:gone:enabled", False)
        with self.assertRaises(webui.PanelError):
            panel.apply_toggle(raw, "account:home:account_number", False)


class FakeSupervisor(supervisor.Supervisor):
    """Records notifications and states-API posts instead of calling Home Assistant."""

    def __init__(self) -> None:
        super().__init__("token")
        self.notifications: list[tuple[str, str]] = []
        self.notification_ids: list[str] = []
        self.posts: dict[str, object] = {}
        self.stored: dict[str, object] = {}
        self.written: list[dict[str, object]] = []

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:  # noqa: ARG002
        self.notifications.append((title, message))
        self.notification_ids.append(notification_id)

    def fetch_options(self) -> dict[str, object]:
        if getattr(self, "unreachable", False):
            raise supervisor.SupervisorError("unreachable")
        return dict(self.stored)

    def write_options(self, options: dict[str, object]) -> None:
        self.written.append(options)
        self.stored = dict(options)

    def core_post(self, path: str, body: object) -> object:
        self.posts[path] = body
        return None

    def core_delete(self, path: str) -> None:
        self.posts.pop(path, None)


class ScriptedClient:
    """Stands in for SajClient: each lookup returns or raises the next scripted result."""

    def __init__(self, results: list[object]) -> None:
        self.results = results

    def lookup(self, _account_number: str) -> ranhill.Bill:
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


class SchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = store.Store(Path(self.tmp.name) / "state.json")
        self.sup = FakeSupervisor()
        self.raw = options({"name": "Home", "account_number": "12345678A1234567"})

    def app(self, results: list[object]) -> saj_engine.Engine:
        client = ScriptedClient(results)
        return helpers.engine("saj", self.raw, self.sup, self.store, client_factory=lambda: client)  # type: ignore[return-value]

    def test_success_publishes_amount_and_masks_identity(self) -> None:
        app = self.app([ranhill.Bill(45.6, "RM 45.60", "AHMAD BIN ALI")])
        runtime = app.runtimes[0]
        app.check_account(runtime)
        self.assertEqual(runtime.values["amount_due"], 45.6)
        self.assertEqual(runtime.values["status"], "ok")
        self.assertEqual(runtime.holder, "A•••• B•••• A••••")
        posted = self.sup.posts[f"/states/{mqtt_pub.entity_id('home', 'amount_due')}"]
        self.assertEqual(posted["state"], "45.60")  # type: ignore[index]
        self.assertEqual(posted["attributes"]["account_number"], "1234••••4567")  # type: ignore[index]
        saved = str(self.store.data)
        self.assertNotIn("12345678A1234567", saved)
        self.assertNotIn("AHMAD", saved)

    def test_amount_changed_only_moves_on_change(self) -> None:
        app = self.app([ranhill.Bill(10.0, "RM 10.00"), ranhill.Bill(10.0, "RM 10.00"), ranhill.Bill(12.5, "RM 12.50")])
        runtime = app.runtimes[0]
        app.check_account(runtime)
        first = runtime.values["amount_changed"]
        runtime.values["amount_changed"] = "marker"
        app.check_account(runtime)
        self.assertEqual(runtime.values["amount_changed"], "marker")
        app.check_account(runtime)
        self.assertNotEqual(runtime.values["amount_changed"], "marker")
        self.assertIsNotNone(first)

    def test_transient_errors_notify_after_three_then_recover(self) -> None:
        err = ranhill.SiteError("HTTP 503 from Ranhill SAJ")
        app = self.app([err, err, err, err, ranhill.Bill(1.0, "RM 1.00")])
        runtime = app.runtimes[0]
        for _ in range(2):
            app.check_account(runtime)
        self.assertEqual(self.sup.notifications, [])
        app.check_account(runtime)
        app.check_account(runtime)
        self.assertEqual(len(self.sup.notifications), 1)
        app.check_account(runtime)
        self.assertEqual(self.sup.notifications[-1][0], "Ranhill SAJ check recovered")

    def test_unknown_account_notifies_at_once(self) -> None:
        app = self.app([ranhill.AccountNotFoundError("Ranhill SAJ does not recognise this account number")])
        runtime = app.runtimes[0]
        app.check_account(runtime)
        self.assertEqual(runtime.values["status"], "not_found")
        self.assertIn("account number", self.sup.notifications[0][1])

    def test_state_survives_a_restart(self) -> None:
        app = self.app([ranhill.Bill(33.3, "RM 33.30", "AHMAD BIN ALI")])
        app.check_account(app.runtimes[0])
        again = self.app([])
        self.assertEqual(again.runtimes[0].values["amount_due"], 33.3)
        self.assertEqual(again.runtimes[0].holder, "A•••• B•••• A••••")

    def test_changed_number_starts_fresh(self) -> None:
        app = self.app([ranhill.Bill(33.3, "RM 33.30")])
        app.check_account(app.runtimes[0])
        app.host.apply_config({}, helpers.load("saj", options({"name": "Home", "account_number": "87654321B7654321"})))
        self.assertIsNone(app.runtimes[0].values["amount_due"])
        self.assertEqual(app.runtimes[0].values["status"], "pending")

    def test_paused_account_is_unavailable_and_not_checked(self) -> None:
        self.raw = options({"name": "Home", "account_number": "12345678A1234567", "enabled": False})
        app = self.app([])
        app.runtimes[0].next_check = 0.0
        core = app.host
        core.next_mqtt_check = core.next_phone_check = core.next_options_check = float("inf")
        core.next_old_app_check = float("inf")
        core.republish.set()  # as the first MQTT check does without a broker
        core._tick()  # would pop from an empty script if it checked
        posted = self.sup.posts[f"/states/{mqtt_pub.entity_id('home', 'amount_due')}"]
        self.assertEqual(posted["state"], "unavailable")  # type: ignore[index]

    def bills(self) -> list[str]:
        return [title for title, _ in self.sup.notifications if "bill" in title]

    def test_payment_due_sensor_and_unpaid_notices(self) -> None:
        app = self.app([ranhill.Bill(45.6, "RM 45.60"), ranhill.Bill(45.6, "RM 45.60"), ranhill.Bill(0.0, "RM 0.00")])
        runtime = app.runtimes[0]
        app.check_account(runtime)
        self.assertIs(runtime.values["payment_due"], True)
        posted = self.sup.posts[f"/states/{mqtt_pub.entity_id('home', 'payment_due', 'binary_sensor')}"]
        self.assertEqual(posted["state"], "on")  # type: ignore[index]
        self.assertEqual(self.bills(), ["Water bill due"])
        self.assertIn("RM 45.60", self.sup.notifications[-1][1])
        self.assertTrue(self.sup.notification_ids[-1].endswith("_bill"))
        app.check_account(runtime)  # same bill, reminded less than a week ago: quiet
        self.assertEqual(self.bills(), ["Water bill due"])
        app.check_account(runtime)
        self.assertIs(runtime.values["payment_due"], False)
        self.assertEqual(self.bills(), ["Water bill due", "Water bill paid"])
        self.assertEqual(runtime.unpaid_notified_at, "")

    def test_unpaid_reminder_after_a_week_survives_restart(self) -> None:
        app = self.app([ranhill.Bill(20.0, "RM 20.00")])
        app.check_account(app.runtimes[0])
        app.runtimes[0].unpaid_notified_at = "2020-01-01T00:00:00+00:00"
        app._save_runtime(app.runtimes[0])
        again = self.app([ranhill.Bill(20.0, "RM 20.00")])
        again.check_account(again.runtimes[0])
        self.assertEqual(self.bills(), ["Water bill due", "Water bill still unpaid"])

    def test_unpaid_notices_can_be_switched_off(self) -> None:
        self.raw = options({"name": "Home", "account_number": "12345678A1234567"}, notify_on_unpaid=False)
        app = self.app([ranhill.Bill(20.0, "RM 20.00")])
        app.check_account(app.runtimes[0])
        self.assertEqual(self.sup.notifications, [])

    def test_weekly_schedule_survives_a_restart(self) -> None:
        app = self.app([ranhill.Bill(20.0, "RM 20.00")])
        app.check_account(app.runtimes[0])
        again = self.app([])
        wait = again.runtimes[0].next_check - saj_engine.time.monotonic()
        self.assertGreater(wait, saj_engine.CHECK_INTERVAL_SECONDS - 3600)
        fresh = helpers.engine("saj", self.raw, self.sup, store.Store(Path(self.tmp.name) / "other.json"))
        self.assertLess(fresh.runtimes[0].next_check - saj_engine.time.monotonic(), 60)  # type: ignore[attr-defined]

    def test_unknown_account_retries_daily(self) -> None:
        app = self.app([ranhill.AccountNotFoundError("unknown")])
        app.check_account(app.runtimes[0])
        wait = app.runtimes[0].next_check - saj_engine.time.monotonic()
        self.assertLessEqual(wait, saj_engine.PROBLEM_RETRY_SECONDS)

    def test_panel_switch_without_the_supervisor_writes_nothing(self) -> None:
        app = self.app([])
        self.sup.unreachable = True  # type: ignore[attr-defined]
        with self.assertRaises(webui.PanelError):
            app.host.panel_toggle("mqtt", False)
        self.assertEqual(self.sup.written, [])

    def test_panel_switch_writes_secret_references(self) -> None:
        self.sup.stored = helpers.merged("saj", options({"name": "Home", "account_number": "!secret saj_home"}))
        app = self.app([])
        resolved = helpers.merged("saj", options({"name": "Home", "account_number": "12345678A1234567"}))
        app.host.secrets.learn(self.sup.stored, resolved)
        app.host.panel_toggle("mqtt", False)
        self.assertEqual(self.sup.written[-1]["saj_accounts"][0]["account_number"], "!secret saj_home")  # type: ignore[index]

    def test_panel_state_builds(self) -> None:
        app = self.app([ranhill.SiteError("boom")])
        app.check_account(app.runtimes[0])
        state = app_panel.build_state(app.host)
        card = state["sections"][0]["cards"][0]  # type: ignore[index]
        self.assertEqual(card["badge"]["text"], "Site error")
        self.assertEqual(card["notice"]["text"], "boom")
        self.assertEqual(app.panel_action("check", {"account": "home"}), "Checking Home...")


class DiscoveryTest(unittest.TestCase):
    def test_sensor_config(self) -> None:
        account = config.load_config(options({"name": "Home", "account_number": "12345678A1234567"})).accounts[0]
        payload = mqtt_pub.sensor_config(account, mqtt_pub.SENSORS[0], "0.1.0")
        self.assertEqual(payload["default_entity_id"], "sensor.saj_home_amount_due")
        self.assertEqual(payload["unit_of_measurement"], "MYR")
        self.assertEqual(payload["json_attributes_topic"], "ranhill_saj/home/amount_due/attributes")
        self.assertNotIn("12345678", str(payload))

    def test_payment_due_is_a_binary_sensor(self) -> None:
        account = config.load_config(options({"name": "Home", "account_number": "12345678A1234567"})).accounts[0]
        sensor = next(s for s in mqtt_pub.SENSORS if s.key == "payment_due")
        payload = mqtt_pub.sensor_config(account, sensor, "0.2.0")
        self.assertEqual(payload["default_entity_id"], "binary_sensor.saj_home_payment_due")
        self.assertIn("ON", str(payload["value_template"]))
        self.assertEqual(mqtt_pub.state_value(sensor, False), "off")
        self.assertEqual(mqtt_pub.state_value(sensor, None), "unknown")


class UnpaidTest(unittest.TestCase):
    def test_notice_rules(self) -> None:
        now = datetime(2026, 1, 10, tzinfo=UTC)
        week_ago = now - unpaid.REMIND_EVERY
        self.assertEqual(unpaid.unpaid_notice(None, 10.0, None, now), unpaid.NEW)
        self.assertEqual(unpaid.unpaid_notice(10.0, 12.0, now, now), unpaid.NEW)
        self.assertIsNone(unpaid.unpaid_notice(10.0, 10.0, now, now))
        self.assertEqual(unpaid.unpaid_notice(10.0, 10.0, week_ago, now), unpaid.REMINDER)
        self.assertEqual(unpaid.unpaid_notice(10.0, 0.0, now, now), unpaid.PAID)
        self.assertIsNone(unpaid.unpaid_notice(0.0, 0.0, None, now))
        self.assertIsNone(unpaid.unpaid_notice(10.0, None, now, now))
        self.assertIs(unpaid.is_due(-5.0), False)  # a credit is not due


if __name__ == "__main__":
    unittest.main()
