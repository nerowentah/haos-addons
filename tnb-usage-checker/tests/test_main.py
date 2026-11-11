"""Tests for the statistics import status shown in the panel and diagnostics (no network)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import main
import mytnb
import stats
import store
import supervisor

LOGIN = {"mytnb_username": "you@example.com", "mytnb_password": "secret"}
WINDOW = datetime(2026, 10, 1, 0, 0, tzinfo=main.MYT)


def options(**extra: object) -> dict[str, object]:
    home = {"name": "Home", "tariff": "general", "account_number": "123456789012", **extra}
    return {**LOGIN, "accounts": [home]}


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


class FakeSession:
    """A myTNB session whose fetch is scripted."""

    def __init__(self, data: mytnb.PortalData) -> None:
        self.data = data
        self.requested: list[tuple[datetime, datetime]] = []

    def fetch(self, start: datetime, end: datetime) -> mytnb.PortalData:
        self.requested.append((start, end))
        return self.data


class StatisticsStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = store.Store(Path(self.tmp.name) / "state.json")
        self.sup = FakeSupervisor()

    def app(self, **extra: object) -> main.App:
        app = main.App(config.load_config(options(**extra)), self.sup, self.store)
        app.broker_found = False  # no MQTT: use the states API
        return app

    def test_a_fresh_account_is_pending(self) -> None:
        app = self.app()
        self.assertEqual(app.runtimes[0].values["stats_status"], "pending")

    def test_a_successful_import_is_recorded(self) -> None:
        app = self.app()
        runtime = app.runtimes[0]
        result = stats.ImportResult(usage_rows=720, cost_rows=30)
        with mock.patch.object(main.stats, "sync_statistics", return_value=result):
            app._import_statistics(runtime, WINDOW, mytnb.PortalData(usage=[], cost=[]))

        self.assertEqual(runtime.values["stats_status"], "ok")
        self.assertEqual(runtime.values["stats_rows"], 750)
        self.assertIsNone(runtime.values["stats_error"])
        self.assertIsInstance(datetime.fromisoformat(str(runtime.values["stats_last_import"])), datetime)

    def test_a_failed_import_is_recorded(self) -> None:
        app = self.app()
        runtime = app.runtimes[0]
        error = stats.StatsError("recorder/import_statistics: unknown_error nope")
        with mock.patch.object(main.stats, "sync_statistics", side_effect=error):
            app._import_statistics(runtime, WINDOW, mytnb.PortalData(usage=[], cost=[]))

        self.assertEqual(runtime.values["stats_status"], "error")
        self.assertIsNone(runtime.values["stats_rows"])
        self.assertIn("nope", str(runtime.values["stats_error"]))

    def test_an_unexpected_error_is_recorded_too(self) -> None:
        app = self.app()
        runtime = app.runtimes[0]
        with mock.patch.object(main.stats, "sync_statistics", side_effect=OSError("connection refused")):
            app._import_statistics(runtime, WINDOW, mytnb.PortalData(usage=[], cost=[]))

        self.assertEqual(runtime.values["stats_status"], "error")
        self.assertIn("connection refused", str(runtime.values["stats_error"]))

    def test_long_errors_are_truncated(self) -> None:
        app = self.app()
        runtime = app.runtimes[0]
        with mock.patch.object(main.stats, "sync_statistics", side_effect=stats.StatsError("x" * 500)):
            app._import_statistics(runtime, WINDOW, mytnb.PortalData(usage=[], cost=[]))

        self.assertEqual(len(str(runtime.values["stats_error"])), main.STATS_ERROR_MAX_CHARS)

    def test_a_disabled_import_shows_off(self) -> None:
        app = self.app(statistics_enabled=False)
        runtime = app.runtimes[0]
        self.assertEqual(runtime.values["stats_status"], "off")  # before the first sync too
        runtime.portal = FakeSession(mytnb.PortalData(usage=[], cost=[]))
        app.sync_portal(runtime)

        self.assertEqual(runtime.values["stats_status"], "off")
        self.assertIsNone(runtime.values["stats_last_import"])


if __name__ == "__main__":
    unittest.main()
