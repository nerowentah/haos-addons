"""Tests for the app core: merged options, entity ids kept from the old apps, hand-off and import (no network)."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rootfs" / "app"))

import config
import main
import panel
from core import mqtt, store, supervisor, webui
from core.options import ConfigError
from providers.saj import engine as saj_engine
from providers.tnb import engine as tnb_engine
from providers.tnb import stats

TNB_HOME = {
    "name": "Home",
    "tariff": "tou",
    "billing_start_day": 1,
    "account_number": "123456789012",
    "import_entity": "sensor.grid",
}
SAJ_HOME = {"name": "Home", "account_number": "12345678-A1234567"}
OPTIONS = {
    "mqtt": True,
    "notify_on_failure": True,
    "tnb_mytnb_username": "you@example.com",
    "tnb_mytnb_password": "!secret tnb_pw",
    "tnb_accounts": [TNB_HOME],
    "saj_accounts": [SAJ_HOME],
}
RESOLVED = {**OPTIONS, "tnb_mytnb_password": "pw"}

# What TNB Usage Checker 0.4.1 and Ranhill SAJ Bill Checker 0.2.1 published for these accounts
# (taken from their code before the merge). Entities keep their ids and history only if these match.
OLD_TNB_KEYS = (
    "portal_usage_cycle", "portal_cost_cycle", "portal_usage_latest_day", "portal_cost_latest_day",
    "amount_due", "payment_due", "due_date", "portal_data_as_of", "portal_status", "portal_last_sync",
    "bill_estimate", "predicted_bill", "predicted_kwh", "cycle_import", "cycle_peak", "cycle_offpeak",
    "cycle_export", "today_import", "today_export", "current_rate", "current_period", "tier",
    "days_remaining", "nem_carry_kwh",
)  # fmt: skip
OLD_SAJ_KEYS = ("amount_due", "payment_due", "amount_changed", "status", "last_checked")


def old_ids(base: str, prefix: str, keys: list[str] | tuple[str, ...]) -> list[tuple[str, str, str, str]]:
    rows = []
    for key in keys:
        component = "binary_sensor" if key == "payment_due" else "sensor"
        rows.append(
            (
                f"homeassistant/{component}/{base}/home_{key}/config",
                f"{base}_home_{key}",
                f"{component}.{prefix}_home_{key}",
                f"{base}/home/state",
            )
        )
    return rows


OLD_TNB = [
    *old_ids("power_tnb", "tnb", OLD_TNB_KEYS),
    (
        "homeassistant/binary_sensor/power_tnb/holiday_today/config",
        "power_tnb_holiday_today",
        "binary_sensor.tnb_holiday_today",
        "power_tnb/holiday_today",
    ),
]
OLD_SAJ = old_ids("ranhill_saj", "saj", OLD_SAJ_KEYS)


class FakeSupervisor(supervisor.Supervisor):
    """Options, installed apps and Home Assistant calls, all in memory."""

    def __init__(self) -> None:
        super().__init__("token")
        self.stored: dict[str, object] = copy.deepcopy(OPTIONS)
        self.written: list[dict[str, object]] = []
        self.addons: list[dict[str, object]] = []
        self.old_options: dict[str, dict[str, object]] = {}
        self.posts: dict[str, object] = {}
        self.deleted: list[str] = []
        self.notifications: list[tuple[str, str]] = []

    def fetch_options(self) -> dict[str, object]:
        return copy.deepcopy(self.stored)

    def write_options(self, options: dict[str, object]) -> None:
        self.written.append(copy.deepcopy(options))
        self.stored = copy.deepcopy(options)

    def list_addons(self) -> list[dict[str, object]]:
        return copy.deepcopy(self.addons)

    def addon_options(self, full_slug: str) -> dict[str, object]:
        return copy.deepcopy(self.old_options[full_slug])

    def core_post(self, path: str, body: object) -> object:
        self.posts[path] = body
        return None

    def core_delete(self, path: str) -> None:
        self.deleted.append(path)
        self.posts.pop(path, None)

    def get_state(self, entity_id: str) -> dict[str, object] | None:  # noqa: ARG002
        return None

    def discover_mobile_targets(self) -> list[str] | None:
        return []

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:  # noqa: ARG002
        self.notifications.append((title, notification_id))


class FakePublisher:
    """Records retained MQTT publishes."""

    connected = True

    def __init__(self) -> None:
        self.retained: dict[str, object] = {}
        self.cleared: list[str] = []

    def publish_json(self, topic: str, payload: object, **_: object) -> None:
        if payload is None:
            self.cleared.append(topic)
            self.retained.pop(topic, None)
        else:
            self.retained[topic] = payload

    def publish_raw(self, topic: str, payload: str, **_: object) -> None:
        self.retained[topic] = payload

    def stop(self) -> None:
        return None


class NoSiteClient:
    def lookup(self, _number: str) -> object:
        raise AssertionError("no site calls in these tests")


class AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = store.Store(Path(tmp.name) / "state.json")
        self.sup = FakeSupervisor()

    def make(self, raw: dict[str, object] | None = None) -> main.App:
        secrets = webui.SecretRefs()
        secrets.learn(OPTIONS, RESOLVED)
        stored = raw if raw is not None else self.sup.stored
        app = main.App(
            config.load_config(secrets.resolve(stored)),  # type: ignore[arg-type]
            self.sup,
            self.store,
            copy.deepcopy(stored),
            engine_kwargs={"saj": {"client_factory": NoSiteClient}},
        )
        app.secrets = secrets
        return app

    def with_mqtt(self, app: main.App) -> FakePublisher:
        publisher = FakePublisher()
        app.publisher = publisher  # type: ignore[assignment]
        app.broker_found = True
        return publisher


class ConfigTest(unittest.TestCase):
    def test_providers_are_read_from_their_prefixed_options(self) -> None:
        cfg = config.load_config(RESOLVED)
        self.assertEqual(sorted(cfg.providers), ["saj", "tnb"])
        self.assertEqual(cfg.account_count, 2)
        self.assertEqual(cfg.providers["saj"].accounts[0].account_number, "12345678A1234567")  # type: ignore[attr-defined]
        self.assertEqual(cfg.providers["tnb"].accounts[0].mytnb_password, "pw")  # type: ignore[attr-defined]
        self.assertTrue(cfg.providers["tnb"].notify_on_failure)  # type: ignore[attr-defined]

    def test_a_provider_without_accounts_is_left_out(self) -> None:
        cfg = config.load_config({**RESOLVED, "tnb_accounts": []})
        self.assertEqual(list(cfg.providers), ["saj"])
        self.assertEqual(config.load_config({}).providers, {})  # starts, and the panel offers the import

    def test_errors_name_the_provider(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            config.load_config({"saj_accounts": [{"name": "Home", "account_number": "nope"}]})
        self.assertTrue(str(ctx.exception).startswith("Ranhill SAJ (saj_ options): "))

    def test_import_renames_known_keys_and_drops_the_rest(self) -> None:
        old = {
            "accounts": [TNB_HOME],
            "afa_rate": 0.01,
            "mqtt": False,
            "notification_overrides": [],
            "mytnb_password": "!secret tnb_pw",
        }
        imported = config.import_options(config.PROVIDERS_BY_KEY["tnb"], old)
        self.assertEqual(
            imported, {"tnb_accounts": [TNB_HOME], "tnb_afa_rate": 0.01, "tnb_mytnb_password": "!secret tnb_pw"}
        )

    def test_every_provider_option_is_in_the_app_schema(self) -> None:
        schema = (Path(__file__).resolve().parents[2] / "config.yaml").read_text(encoding="utf-8")
        for spec in config.PROVIDERS:
            for key in spec.option_keys:
                self.assertIn(f"  {spec.prefix}{key}:", schema)


class EntityIdTest(AppTestCase):
    def rows(self, engine: object) -> list[tuple[str, str, str, str]]:
        return [
            (topic, str(p["unique_id"]), str(p["default_entity_id"]), str(p["state_topic"]))
            for topic, p in engine.discovery()  # type: ignore[attr-defined]
        ]

    def test_ids_match_the_old_apps(self) -> None:
        app = self.make()
        self.assertEqual(self.rows(app.engines["tnb"]), OLD_TNB)
        self.assertEqual(self.rows(app.engines["saj"]), OLD_SAJ)
        self.assertEqual(stats.SOURCE, "power_tnb")  # long-term statistics keep their ids

    def test_one_bridge_availability_topic(self) -> None:
        app = self.make()
        for engine in app.engines.values():
            for _, payload in engine.discovery():
                topics = [payload["availability_topic"]] if "availability_topic" in payload else []
                topics += [a["topic"] for a in payload.get("availability", [])]  # type: ignore[union-attr]
                self.assertIn(mqtt.AVAILABILITY_TOPIC, topics)
                self.assertEqual(payload["origin"]["name"], "Malaysia Utility Bills")  # type: ignore[index]
        self.assertNotIn(mqtt.CLIENT_ID, ("power_tnb", "ranhill_saj"))  # never kicks an old app off the broker

    def test_discovery_publishes_both_providers(self) -> None:
        app = self.make()
        publisher = self.with_mqtt(app)
        app.publish_discovery()
        topics = set(self.store.data["discovery_topics"])  # type: ignore[arg-type]
        self.assertEqual(topics, {row[0] for row in OLD_TNB + OLD_SAJ})
        self.assertIn("power_tnb/home/state", publisher.retained)
        self.assertIn("ranhill_saj/home/state", publisher.retained)

    def test_removed_provider_is_cleared(self) -> None:
        app = self.make()
        publisher = self.with_mqtt(app)
        app.publish_discovery()
        app.apply_config({}, config.load_config({**RESOLVED, "saj_accounts": []}))
        self.assertNotIn("saj", app.engines)
        self.assertEqual(sorted(publisher.cleared), sorted(row[0] for row in OLD_SAJ))

    def test_state_is_kept_per_provider(self) -> None:
        app = self.make()
        app.engines["saj"]._save_runtime(app.engines["saj"].runtimes[0])  # type: ignore[attr-defined]
        self.assertIn("home", self.store.data["saj"]["accounts"])  # type: ignore[index]
        self.assertNotIn("accounts", self.store.data)


class HandOffTest(AppTestCase):
    def old_app(self, state: str) -> None:
        self.sup.addons = [{"slug": "a1b2c3d4_power_tnb", "name": "TNB Usage Checker", "state": state}]

    def test_running_old_app_blocks_its_provider_only(self) -> None:
        self.old_app("started")
        app = self.make()
        app.check_old_apps()
        self.assertEqual([spec.key for spec in app.blocked()], ["tnb"])
        self.assertEqual([e.key for e in app.active_engines()], ["saj"])
        with self.assertRaises(webui.PanelError):
            app.panel_action("tnb:sync", {"account": "home"})
        cards = panel.build_state(app)["sections"][0]["cards"]  # type: ignore[index]
        self.assertEqual(cards[0]["title"], "Waiting for TNB Usage Checker to stop")  # type: ignore[index]

    def test_blocked_provider_never_clears_the_old_apps_entities(self) -> None:
        app = self.make()
        publisher = self.with_mqtt(app)
        app.publish_discovery()  # took over before ...
        self.old_app("started")  # ... then the old app was started again
        app.check_old_apps()
        app.publish_discovery()
        self.assertEqual(publisher.cleared, [])
        self.assertEqual(set(self.store.data["discovery_topics"]), {row[0] for row in OLD_TNB + OLD_SAJ})  # type: ignore[arg-type]
        app.config.mqtt = False
        app._retire_mqtt()
        self.assertEqual(sorted(publisher.cleared), sorted(row[0] for row in OLD_SAJ))

    def test_blocked_provider_keeps_the_old_apps_states_api_entities(self) -> None:
        self.store.data["rest_entities"] = ["sensor.tnb_home_bill_estimate", "sensor.saj_gone_amount_due"]
        self.old_app("started")
        app = self.make()
        app.broker_found = False
        app.check_old_apps()
        app.publish_discovery()
        self.assertEqual(self.sup.deleted, ["/states/sensor.saj_gone_amount_due"])
        self.assertFalse(any(path.startswith("/states/sensor.tnb_") for path in self.sup.posts))

    def test_takes_over_once_the_old_app_stops(self) -> None:
        self.old_app("started")
        app = self.make()
        app.check_old_apps()
        self.old_app("stopped")
        app.republish.clear()
        app.check_old_apps()
        self.assertEqual(app.blocked(), [])
        self.assertTrue(app.republish.is_set())


class ImportTest(AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sup.addons = [{"slug": "local_power_tnb", "name": "TNB Usage Checker", "state": "started"}]
        self.sup.old_options["local_power_tnb"] = {
            "accounts": [TNB_HOME],
            "mytnb_username": "you@example.com",
            "mytnb_password": "!secret tnb_pw",
            "afa_rate": 0.01,
            "notification_overrides": [],
            "mqtt": False,
        }

    def test_import_copies_options_and_keeps_secret_references(self) -> None:
        self.sup.stored = {"mqtt": True, "tnb_accounts": [], "saj_accounts": [SAJ_HOME]}
        app = self.make()
        message = app.panel_action("import", {"provider": "tnb"})
        written = self.sup.written[-1]
        self.assertEqual(written["tnb_accounts"], [TNB_HOME])
        self.assertEqual(written["tnb_mytnb_password"], "!secret tnb_pw")
        self.assertEqual(written["tnb_afa_rate"], 0.01)
        self.assertIs(written["mqtt"], True)  # shared switches stay as they are
        self.assertNotIn("tnb_notification_overrides", written)
        self.assertIn("Stop TNB Usage Checker", str(message))

    def test_unknown_secret_is_saved_for_the_next_start(self) -> None:
        self.sup.stored = {"tnb_accounts": [], "saj_accounts": [SAJ_HOME]}
        self.sup.old_options["local_power_tnb"]["mytnb_password"] = "!secret only_in_old_app"
        app = self.make()
        message = app.panel_action("import", {"provider": "tnb"})
        self.assertEqual(self.sup.written[-1]["tnb_mytnb_password"], "!secret only_in_old_app")
        self.assertIn("restart", str(message))
        self.assertIsNone(app.pending)

    def test_import_refuses_to_overwrite_accounts(self) -> None:
        app = self.make()
        with self.assertRaises(webui.PanelError):
            app.panel_action("import", {"provider": "tnb"})
        self.assertEqual(self.sup.written, [])

    def test_missing_old_app(self) -> None:
        self.sup.addons = []
        app = self.make()
        with self.assertRaises(webui.PanelError):
            app.panel_action("import", {"provider": "saj"})


class RoutingTest(AppTestCase):
    def test_ids_are_namespaced_per_provider(self) -> None:
        app = self.make()
        state = panel.build_state(app)
        tnb_card = state["sections"][0]["cards"][0]  # type: ignore[index]
        saj_card = state["sections"][1]["cards"][0]  # type: ignore[index]
        self.assertEqual(tnb_card["toggles"][0]["id"], "tnb:account:home:enabled")
        self.assertEqual(saj_card["toggles"][0]["id"], "saj:account:home:enabled")
        self.assertEqual(saj_card["actions"][0]["id"], "saj:check")

    def test_toggles_and_actions_reach_the_right_provider(self) -> None:
        app = self.make()
        app.panel_toggle("saj:account:home:enabled", False)
        self.assertIs(self.sup.written[-1]["saj_accounts"][0]["enabled"], False)  # type: ignore[index]
        self.assertIsNot(self.sup.written[-1]["tnb_accounts"][0].get("enabled"), False)  # type: ignore[index]
        app.panel_toggle("tnb:daily_report", False)
        self.assertIs(self.sup.written[-1]["tnb_daily_report"], False)
        self.assertEqual(app.panel_action("saj:check", {"account": "home"}), "Checking Home...")
        with self.assertRaises(webui.PanelError):
            app.panel_toggle("tnb:mqtt", False)
        with self.assertRaises(webui.PanelError):
            app.panel_action("check", {"account": "home"})

    def test_empty_app_offers_the_import(self) -> None:
        self.sup.addons = [{"slug": "local_ranhill_saj_bill_checker", "name": "Ranhill SAJ", "state": "started"}]
        app = self.make({})
        app.check_old_apps()
        state = panel.build_state(app)
        self.assertEqual(state["sections"][0]["title"], "Get started")  # type: ignore[index]
        move = state["sections"][-1]["cards"][-1]  # type: ignore[index]
        self.assertEqual(move["actions"][0]["args"], {"provider": "saj"})


class SchedulerTest(AppTestCase):
    def test_tick_runs_every_provider(self) -> None:
        app = self.make()
        app.broker_found = False
        app.next_mqtt_check = app.next_phone_check = app.next_options_check = float("inf")
        calls: list[str] = []
        tnb = app.engines["tnb"]
        saj = app.engines["saj"]
        tnb.tick = lambda: calls.append("tnb")  # type: ignore[method-assign]
        saj.tick = lambda: calls.append("saj")  # type: ignore[method-assign]
        app._tick()
        self.assertEqual(calls, ["tnb", "saj"])
        self.assertIsInstance(tnb, tnb_engine.Engine)
        self.assertIsInstance(saj, saj_engine.Engine)

    def test_notifications_follow_the_shared_switches(self) -> None:
        app = self.make({**OPTIONS, "notify_on_failure": False})
        engine = app.engines["saj"]
        engine.notify(engine.runtimes[0].account, "failed", "x")  # type: ignore[attr-defined]
        engine.notify(engine.runtimes[0].account, "bill", "x", bill=True)  # type: ignore[attr-defined]
        self.assertEqual(self.sup.notifications, [("bill", "ranhill_saj_home_bill")])


if __name__ == "__main__":
    unittest.main()
