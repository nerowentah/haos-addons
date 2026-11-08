"""Tests for options validation, masking, panel toggles and the check scheduler (no network)."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import main
import mqtt_pub
import panel
import ranhill
import store
import supervisor
import webui


def options(*accounts: dict[str, object], **extra: object) -> dict[str, object]:
    return {"poll_interval_hours": 6, "notify_on_failure": True, "mqtt": True, "accounts": list(accounts), **extra}


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

    def test_interval_is_clamped(self) -> None:
        cfg = config.load_config(
            options({"name": "Home", "account_number": "12345678A1234567"}, poll_interval_hours=99)
        )
        self.assertEqual(cfg.poll_interval_hours, 24)

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
    def test_account_and_global_toggles(self) -> None:
        raw = options({"name": "Home", "account_number": "12345678A1234567"})
        panel.apply_toggle(raw, "account:home:enabled", False)
        panel.apply_toggle(raw, "mqtt", False)
        self.assertIs(raw["accounts"][0]["enabled"], False)  # type: ignore[index]
        self.assertIs(raw["mqtt"], False)

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
        self.posts: dict[str, object] = {}

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:  # noqa: ARG002
        self.notifications.append((title, message))

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
        self.cfg = config.load_config(options({"name": "Home", "account_number": "12345678A1234567"}))

    def app(self, results: list[object]) -> main.App:
        client = ScriptedClient(results)
        app = main.App(self.cfg, self.sup, self.store, client_factory=lambda: client)  # type: ignore[arg-type,return-value]
        app.broker_found = False  # no MQTT: use the states API
        return app

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
        self.cfg = config.load_config(options({"name": "Home", "account_number": "87654321B7654321"}))
        app.apply_config({}, self.cfg)
        self.assertIsNone(app.runtimes[0].values["amount_due"])
        self.assertEqual(app.runtimes[0].values["status"], "pending")

    def test_paused_account_is_unavailable_and_not_checked(self) -> None:
        self.cfg = config.load_config(options({"name": "Home", "account_number": "12345678A1234567", "enabled": False}))
        app = self.app([])
        app.runtimes[0].next_check = 0.0
        app.next_mqtt_check = app.next_phone_check = app.next_options_check = float("inf")
        app.republish.set()  # as the first MQTT check does without a broker
        app._tick()  # would pop from an empty script if it checked
        posted = self.sup.posts[f"/states/{mqtt_pub.entity_id('home', 'amount_due')}"]
        self.assertEqual(posted["state"], "unavailable")  # type: ignore[index]

    def test_panel_state_builds(self) -> None:
        app = self.app([ranhill.SiteError("boom")])
        app.check_account(app.runtimes[0])
        state = panel.build_state(app)
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


if __name__ == "__main__":
    unittest.main()
