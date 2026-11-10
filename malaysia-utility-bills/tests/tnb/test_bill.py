"""Tests for the amount due: account page fetch, entities, and unpaid-bill notices (no network)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from string import Template

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rootfs" / "app"))

from core import store, supervisor, unpaid
from providers.tnb import config, mqtt_pub, mytnb, panel
from providers.tnb import engine as tnb_engine

from tests import helpers

LOGIN = {"mytnb_username": "you@example.com", "mytnb_password": "secret"}


def options(**extra: object) -> dict[str, object]:
    home = {"name": "Home", "tariff": "general", "billing_start_day": 1, "account_number": "123456789012"}
    return {**LOGIN, "accounts": [home], **extra}


class FakeSupervisor(supervisor.Supervisor):
    """Records notifications and states-API posts instead of calling Home Assistant."""

    def __init__(self) -> None:
        super().__init__("token")
        self.notifications: list[tuple[str, str, str]] = []
        self.posts: dict[str, object] = {}

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:  # noqa: ARG002
        self.notifications.append((title, message, notification_id))

    def core_post(self, path: str, body: object) -> object:
        self.posts[path] = body
        return None

    def core_delete(self, path: str) -> None:
        self.posts.pop(path, None)


FIXTURE = Template((Path(__file__).parent / "fixtures" / "dashboard.html").read_text(encoding="utf-8"))
DUE = {"headline": "My Amount Due", "amount": "13.90", "due": "Due Date: 23-Nov-2026"}
CLEARED = {"headline": "I&#39;ve cleared all bills", "amount": "0.00", "due": "&nbsp;"}
CREDIT = {"headline": "I've paid extra", "amount": "63.70", "due": "&nbsp;"}


def dashboard(selected: str = "123456789012", **card: str) -> str:
    return FIXTURE.substitute(selected=selected, **card)


def _ok(body: str, url: str = mytnb.BILL_DASHBOARD_URL) -> mytnb._Response:
    return mytnb._Response(status=200, body=body, content_type="text/html", url=url)


class ScriptedSession(mytnb.MyTnbSession):
    """Answers each request with the next scripted response (no network)."""

    def __init__(self, ca_no: str, responses: list[mytnb._Response | str], bill_no: str = "") -> None:
        super().__init__("you@example.com", "pw", ca_no, bill_no)
        self.responses = [_ok(r) if isinstance(r, str) else r for r in responses]
        self.requested: list[str] = []

    def _send_raw(self, url, *, data=None, headers=None):  # type: ignore[override]  # noqa: ARG002
        self.requested.append(url)
        return self.responses.pop(0)


class BillAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = store.Store(Path(self.tmp.name) / "state.json")
        self.sup = FakeSupervisor()

    def app(self, **extra: object) -> tnb_engine.Engine:
        return helpers.engine("tnb", options(**extra), self.sup, self.store)  # type: ignore[return-value]

    def titles(self) -> list[str]:
        return [title for title, _, _ in self.sup.notifications]

    def test_amount_due_entities_and_notices(self) -> None:
        app = self.app()
        runtime = app.runtimes[0]
        app._bill_succeeded(runtime, mytnb.BillInfo(88.5, date(2026, 12, 1)))
        app.publish_state(runtime)
        self.assertEqual(runtime.values["amount_due"], 88.5)
        self.assertEqual(runtime.values["due_date"], "2026-12-01")
        posted = self.sup.posts[f"/states/{mqtt_pub.entity_id('home', 'payment_due', 'binary_sensor')}"]
        self.assertEqual(posted["state"], "on")  # type: ignore[index]
        self.assertEqual(self.titles(), ["TNB bill due"])
        self.assertIn("RM 88.50", self.sup.notifications[0][1])
        self.assertIn("01 Dec 2026", self.sup.notifications[0][1])
        self.assertNotIn("123456789012", self.sup.notifications[0][1])
        self.assertTrue(self.sup.notifications[0][2].endswith("_bill"))

        app._bill_succeeded(runtime, mytnb.BillInfo(88.5, date(2026, 12, 1)))  # every 6 h sync: no repeat
        self.assertEqual(self.titles(), ["TNB bill due"])

        self.store.section("tnb")["bills"]["home"]["notified_at"] = "2020-01-01T00:00:00+08:00"  # type: ignore[index]
        again = self.app()  # the reminder clock survives a restart
        again._bill_succeeded(again.runtimes[0], mytnb.BillInfo(88.5, date(2026, 12, 1)))
        self.assertEqual(self.titles(), ["TNB bill due", "TNB bill still unpaid"])

        again._bill_succeeded(again.runtimes[0], mytnb.BillInfo(0.0))
        self.assertIs(again.runtimes[0].values["payment_due"], False)
        self.assertEqual(self.titles()[-1], "TNB bill paid")

    def test_unpaid_notices_can_be_switched_off(self) -> None:
        app = self.app(notify_on_unpaid=False)
        app._bill_succeeded(app.runtimes[0], mytnb.BillInfo(10.0))
        self.assertEqual(self.sup.notifications, [])
        self.assertIs(app.runtimes[0].values["payment_due"], True)

    def test_card_shows_amount_due(self) -> None:
        app = self.app()
        runtime = app.runtimes[0]
        app._bill_succeeded(runtime, mytnb.BillInfo(12.3, date(2026, 12, 1)))
        stats = {s["label"]: s["value"] for s in panel.account_cards(app, app.config)[0]["stats"]}  # type: ignore[index]
        self.assertEqual(stats["Amount due"], "RM 12.30")
        self.assertEqual(stats["Payment"], "Due by 01 Dec")

    def test_meter_only_account_has_no_bill_entities(self) -> None:
        entry = {"name": "Shop", "tariff": "general", "billing_start_day": 1, "import_entity": "sensor.grid"}
        account = config.load_config({"accounts": [entry]}).accounts[0]
        self.assertNotIn("payment_due", [s.key for s in mqtt_pub.sensors_for(account)])


class BillFetchTest(unittest.TestCase):
    def test_account_page_failure_does_not_fail_the_sync(self) -> None:
        session = mytnb.MyTnbSession("you@example.com", "pw", "123456789012")

        def boom(*_args: object, **_kwargs: object) -> mytnb._Response:
            raise mytnb.PortalError("timed out")

        session._send_raw = boom  # type: ignore[method-assign]
        with self.assertLogs(mytnb.__name__, "WARNING"):
            self.assertIsNone(session.fetch_bill())

    def test_shown_account_is_read_without_switching(self) -> None:
        session = ScriptedSession("123456789012", [dashboard("123456789012", **DUE)])
        self.assertEqual(session.fetch_bill(), mytnb.BillInfo(13.9, date(2026, 11, 23)))
        self.assertEqual(len(session.requested), 1)

    def test_switches_to_this_account_first(self) -> None:
        pages = [dashboard("123456789012", **CLEARED), _ok("false"), dashboard("123456789014", **CREDIT)]
        session = ScriptedSession("AbC%2B12%3D%3D", pages, bill_no="123456789014")
        self.assertEqual(session.fetch_bill(), mytnb.BillInfo(-63.7))
        self.assertIn("selectedAccountID=33333333-3333-4333-8333-333333333333", session.requested[1])

    def test_switch_that_does_not_stick_reads_nothing(self) -> None:
        pages = [dashboard("123456789012", **DUE), _ok("false"), dashboard("123456789012", **DUE)]
        session = ScriptedSession("123456789013", pages)
        with self.assertLogs(mytnb.__name__, "WARNING"):
            self.assertIsNone(session.fetch_bill())

    def test_unlisted_and_prepaid_accounts_read_nothing(self) -> None:
        with self.assertLogs(mytnb.__name__, "WARNING"):
            self.assertIsNone(ScriptedSession("999999999999", [dashboard("123456789012", **DUE)]).fetch_bill())
        prepaid = ScriptedSession("123456789013", [dashboard("123456789012", **DUE), _ok("true")])
        with self.assertLogs(mytnb.__name__, "WARNING"):
            self.assertIsNone(prepaid.fetch_bill())

    def test_encoded_ca_without_bill_number_needs_account_number(self) -> None:
        session = ScriptedSession("AbC%2B12%3D%3D", [dashboard("123456789012", **DUE)])
        with self.assertLogs(mytnb.__name__, "WARNING") as logs:
            self.assertIsNone(session.fetch_bill())
        self.assertIn("account_number", logs.output[0])

    def test_login_page_is_an_error_not_a_bill(self) -> None:
        session = ScriptedSession("123456789012", [_ok("<html></html>", "https://www.mytnb.com.my/Account/Login")])
        with self.assertLogs(mytnb.__name__, "WARNING"):
            self.assertIsNone(session.fetch_bill())


class ParseBillTest(unittest.TestCase):
    def test_three_card_variants(self) -> None:
        self.assertEqual(mytnb.parse_bill(dashboard(**DUE)), mytnb.BillInfo(13.9, date(2026, 11, 23)))
        self.assertEqual(mytnb.parse_bill(dashboard(**CLEARED)), mytnb.BillInfo(0.0))
        self.assertEqual(mytnb.parse_bill(dashboard(**CREDIT)), mytnb.BillInfo(-63.7))
        self.assertEqual(
            mytnb.parse_bill(dashboard(headline="My Amount Due", amount="1,234.50", due="Due Date: 01-Dec-2026")),
            mytnb.BillInfo(1234.5, date(2026, 12, 1)),
        )

    def test_page_without_a_bill_card(self) -> None:
        self.assertIsNone(mytnb.parse_bill("<html><title>Signing In...</title></html>"))

    def test_account_list_and_selection(self) -> None:
        page = dashboard("123456789013", **DUE)
        accounts = mytnb.parse_account_list(page)
        self.assertEqual(accounts["123456789013"], "22222222-2222-4222-8222-222222222222")
        self.assertEqual(len(accounts), 3)
        self.assertEqual(mytnb.selected_account(page), "123456789013")


class BinarySensorTest(unittest.TestCase):
    def test_discovery_and_states(self) -> None:
        account = config.load_config(options()).accounts[0]
        sensor = next(s for s in mqtt_pub.SENSORS if s.key == "payment_due")
        payload = mqtt_pub.sensor_config(account, sensor, "test")
        self.assertEqual(payload["default_entity_id"], "binary_sensor.tnb_home_payment_due")
        self.assertIn("ON", str(payload["value_template"]))
        self.assertEqual(mqtt_pub.state_value(sensor, True), "on")

    def test_unpaid_rules_match_the_water_app(self) -> None:
        now = datetime(2026, 1, 10, tzinfo=UTC)
        self.assertEqual(unpaid.unpaid_notice(None, 5.0, None, now), unpaid.NEW)
        self.assertEqual(unpaid.unpaid_notice(5.0, 5.0, now - unpaid.REMIND_EVERY, now), unpaid.REMINDER)


if __name__ == "__main__":
    unittest.main()
