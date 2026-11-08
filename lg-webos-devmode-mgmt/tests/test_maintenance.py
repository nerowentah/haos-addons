"""Tests for LG renewals around vendor maintenance (windows and back-off)."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import maintenance
import run

OPTIONS = {"interval_hours": 168, "retries": 0, "notify_on_failure": True, "sessions": [{"name": "TV", "token": "t"}]}


class LgMaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._renew, self._notify = run.renew_session, run.notify_failure
        self.notified: list[str] = []
        run.notify_failure = lambda session, _targets, _token: self.notified.append(session.name)

    def tearDown(self) -> None:
        run.renew_session, run.notify_failure = self._renew, self._notify
        self.tmp.cleanup()

    def app(self, windows: list[str] | None = None) -> run.App:
        raw = dict(OPTIONS) if windows is None else {**OPTIONS, "maintenance_windows": windows}
        app = run.App(run.load_config(raw), raw, "token", phones_path=Path(self.tmp.name) / "phones.json")
        app.check_options = lambda: None  # type: ignore[method-assign]
        app.refresh_phones = lambda: None  # type: ignore[method-assign]
        return app

    def test_default_window_and_option_errors(self) -> None:
        self.assertEqual(maintenance.describe(run.load_config(OPTIONS).maintenance_windows), "daily 00:00-06:00")
        with self.assertRaises(run.ConfigError):
            run.load_config({**OPTIONS, "maintenance_windows": ["often"]})

    def test_scheduled_renewal_waits_for_the_window_to_end(self) -> None:
        now = datetime.now().astimezone()
        start = (now - timedelta(minutes=5)).strftime("%H:%M")
        end = (now + timedelta(hours=1)).strftime("%H:%M")
        app = self.app([f"daily {start}-{end}"])
        calls: list[str] = []
        run.renew_session = lambda session: calls.append(session.name) or True
        app._tick()
        self.assertEqual(calls, [])  # no call to LG inside the window
        self.assertGreater(app.next_run, time.time() + 50 * 60)
        with self.assertRaises(run.PanelError):
            app.panel_action("renew_all", {})

    def test_maintenance_answer_backs_off_without_alert(self) -> None:
        app = self.app([])

        def down(_session: run.Session) -> bool:
            raise run.VendorMaintenanceError("LG developer site is under maintenance (HTTP 503)")

        run.renew_session = down
        app._tick()
        status = app.status["tv"]
        self.assertTrue(status.maintenance)
        self.assertIsNone(status.last_ok)
        self.assertEqual(self.notified, [])
        self.assertGreater(app.next_run, time.time() + 25 * 60)  # ~30 min back-off
        self.assertIsNone(app.last_scheduled)
        first = app.next_run

        app.next_run = time.time()
        app._tick()
        self.assertGreater(app.next_run - time.time(), (first - time.time()) * 1.5)  # doubled

        run.renew_session = lambda _session: True
        app.next_run = time.time()
        app._tick()
        self.assertFalse(app.status["tv"].maintenance)
        self.assertTrue(app.status["tv"].last_ok)
        self.assertEqual(app.backoff.attempts, 0)
        self.assertGreater(app.next_run, time.time() + 167 * 3600)  # back on the normal interval

    def test_http_503_is_maintenance(self) -> None:
        import urllib.error  # noqa: PLC0415

        def raise_503(*_args: object, **_kwargs: object) -> object:
            raise urllib.error.HTTPError("https://example.invalid", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]

        original = run.urllib.request.urlopen
        run.urllib.request.urlopen = raise_503  # type: ignore[assignment]
        try:
            with self.assertRaises(run.VendorMaintenanceError):
                self._renew(run.load_config(OPTIONS).sessions[0])
        finally:
            run.urllib.request.urlopen = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
