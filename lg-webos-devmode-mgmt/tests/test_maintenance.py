"""Tests for LG renewals around vendor maintenance (windows and back-off)."""

from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import maintenance
import run
import webui

OPTIONS: dict[str, object] = {
    "interval_hours": 168,
    "retries": 0,
    "notify_on_failure": True,
    "sessions": [{"name": "TV", "token": "t"}],
}


class LgMaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notified: list[str] = []

        def notify(session: run.Session, _targets: list[str], _token: str) -> None:
            self.notified.append(session.name)

        self.patch(run, "notify_failure", notify)

    def patch(self, target: object, name: str, replacement: object) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, windows: list[str] | None = None) -> run.App:
        raw = dict(OPTIONS) if windows is None else {**OPTIONS, "maintenance_windows": windows}
        app = run.App(run.load_config(raw), raw, "token", phones_path=Path(self.tmp.name) / "phones.json")
        self.patch(app, "check_options", lambda: None)
        self.patch(app, "refresh_phones", lambda: None)
        return app

    def test_default_window_and_option_errors(self) -> None:
        self.assertEqual(maintenance.describe(list(run.load_config(OPTIONS).maintenance_windows)), "daily 00:00-06:00")
        with self.assertRaises(run.ConfigError):
            run.load_config({**OPTIONS, "maintenance_windows": ["often"]})

    def test_scheduled_renewal_waits_for_the_window_to_end(self) -> None:
        now = datetime.now().astimezone()
        start = (now - timedelta(minutes=5)).strftime("%H:%M")
        end = (now + timedelta(hours=1)).strftime("%H:%M")
        app = self.app([f"daily {start}-{end}"])
        calls: list[str] = []

        def renew(session: run.Session) -> bool:
            calls.append(session.name)
            return True

        self.patch(run, "renew_session", renew)
        app._tick()
        self.assertEqual(calls, [])  # no call to LG inside the window
        self.assertGreater(app.next_run, time.time() + 50 * 60)
        with self.assertRaises(webui.PanelError):
            app.panel_action("renew_all", webui.PanelArgs())

    def test_maintenance_answer_backs_off_without_alert(self) -> None:
        app = self.app([])

        def down(_session: run.Session) -> bool:
            raise run.VendorMaintenanceError("LG developer site is under maintenance (HTTP 503)")

        self.patch(run, "renew_session", down)
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

        self.patch(run, "renew_session", lambda _session: True)
        app.next_run = time.time()
        app._tick()
        self.assertFalse(app.status["tv"].maintenance)
        self.assertTrue(app.status["tv"].last_ok)
        self.assertEqual(app.backoff.attempts, 0)
        self.assertGreater(app.next_run, time.time() + 167 * 3600)  # back on the normal interval

    def test_http_503_is_maintenance(self) -> None:
        opened: list[object] = []

        def raise_503(url: object, **_kwargs: object) -> object:
            opened.append(url)
            raise urllib.error.HTTPError("https://example.invalid", 503, "Service Unavailable", Message(), io.BytesIO())

        self.patch(urllib.request, "urlopen", raise_503)
        with self.assertRaises(run.VendorMaintenanceError):
            run.renew_session(run.load_config(OPTIONS).sessions[0])
        self.assertEqual(opened, [f"{run.LG_DEV_ENDPOINT}?sessionToken=t"])  # the real address is what is requested

    def test_renew_button_needs_a_known_tv(self) -> None:
        app = self.app([])
        self.assertEqual(app.panel_action("renew", webui.PanelArgs(session="tv")), "Renewing TV...")
        for bad in ({}, {"session": 3}, {"session": "gone"}):
            with self.assertRaises(webui.PanelError):
                app.panel_action("renew", webui.PanelArgs(bad))


if __name__ == "__main__":
    unittest.main()
