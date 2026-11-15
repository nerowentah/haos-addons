"""Tests for the alert when LG says a session has less than LOW_SESSION_HOURS left (no network)."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Self
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import run

OK = '{"result":"success","errorCode":"200","errorMsg":"GNL"}'
OPTIONS: dict[str, object] = {
    "interval_hours": 168,
    "retries": 0,
    "notify_on_failure": True,
    "maintenance_windows": [],
    "sessions": [{"name": "TV", "token": "placeholder-token"}],
}


class Reply:
    """A canned urlopen answer."""

    def __init__(self, body: str, url: str) -> None:
        self.body, self.url, self.status = body, url, 200

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body.encode()

    def geturl(self) -> str:
        return self.url


class LowSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notified: list[str] = []
        self.left = "999:30:00"  # what LG's status address answers
        self.renewal = OK

        def notify(_session: run.Session, _targets: list[str], _token: str, reason: str = "failed") -> None:
            self.notified.append(reason)

        def urlopen(url: str, **_kwargs: object) -> Reply:
            if url.startswith(run.LG_CHECK_ENDPOINT):
                return Reply(f'{{"result":"success","errorCode":"200","errorMsg":"{self.left}"}}', url)
            return Reply(self.renewal, url)

        self.patch(run, "notify_failure", notify)
        self.patch(urllib.request, "urlopen", urlopen)
        self.app = run.App(run.load_config(OPTIONS), OPTIONS, "token", phones_path=Path(self.tmp.name) / "phones.json")
        self.patch(self.app, "check_options", lambda: None)
        self.patch(self.app, "refresh_phones", lambda: None)
        self.patch(self.app.stopping, "wait", lambda _seconds: None)

    def patch(self, target: object, name: str, replacement: object) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def renew(self) -> None:
        self.app.next_run = 0
        self.app._tick()

    def card(self) -> dict[str, object]:
        card: dict[str, object] = self.app.panel_state()["sections"][0]["cards"][0]  # type: ignore[index]
        return card

    def test_hours_left_counts_down_from_lgs_answer(self) -> None:
        status = run.SessionStatus(remaining_hours=200.0, remaining_at=1000.0)
        self.assertEqual(run.hours_left(status, now=1000.0), 200.0)
        self.assertEqual(run.hours_left(status, now=1000.0 + 50 * 3600), 150.0)
        self.assertEqual(run.hours_left(status, now=1000.0 + 500 * 3600), 0.0)
        self.assertEqual(run.hours_left(status, now=0.0), 200.0)  # a clock set back adds nothing
        self.assertIsNone(run.hours_left(run.SessionStatus()))
        self.assertEqual(run.hours_left(run.SessionStatus(remaining_hours=12.0)), 12.0)

    def test_silent_above_the_threshold(self) -> None:
        with self.assertNoLogs(run.__name__, level="WARNING"):
            self.renew()
            self.app._tick()
        self.assertEqual(self.notified, [])
        self.assertEqual(self.card()["badge"], {"text": "Renewed", "tone": "ok"})
        self.assertIn({"label": "Time left", "value": "999", "unit": "h"}, self.card()["stats"])  # type: ignore[arg-type]

    def test_exactly_at_the_threshold_is_not_low(self) -> None:
        self.left = "168:59:59"
        self.renew()
        self.assertEqual(self.notified, [])

    def test_low_answer_warns_and_notifies_once(self) -> None:
        self.left = "145:10:00"
        with self.assertLogs(run.__name__, level="WARNING") as logs:
            self.renew()
            self.app._tick()
            self.renew()  # still low after another renewal: no second alert
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)
        self.assertIn("'TV' has 145 h left", warnings[0])
        self.assertEqual(self.notified, ["low_session"])
        self.assertEqual(self.card()["badge"], {"text": "145 h left", "tone": "warn"})

    def test_crossing_between_renewals_alerts_on_the_next_tick(self) -> None:
        self.left = "200:00:00"
        self.renew()
        self.assertEqual(self.notified, [])
        self.app.status["tv"].remaining_at = time.time() - 40 * 3600  # 40 h later, nothing new from LG
        self.app._tick()
        self.assertEqual(self.notified, ["low_session"])
        self.assertEqual(self.card()["badge"], {"text": "159 h left", "tone": "warn"})
        self.app._tick()
        self.assertEqual(self.notified, ["low_session"])

    def test_armed_again_after_a_good_renewal(self) -> None:
        self.left = "100:00:00"
        self.renew()
        self.left = "999:30:00"
        self.renew()
        self.assertFalse(self.app.status["tv"].low_alerted)
        self.assertEqual(self.card()["badge"], {"text": "Renewed", "tone": "ok"})
        self.left = "100:00:00"
        self.renew()
        self.assertEqual(self.notified, ["low_session", "low_session"])

    def test_failed_renewal_keeps_the_countdown_and_its_own_badge(self) -> None:
        self.left = "100:00:00"
        self.renew()
        self.renewal = ""  # LG answers with nothing: a failure
        self.renew()
        self.assertEqual(self.notified, ["low_session", "failed"])
        self.assertEqual(self.app.status["tv"].remaining_hours, 100.0)
        self.assertEqual(self.card()["badge"], {"text": "Failed", "tone": "error"})

    def test_no_phone_alert_when_notifications_are_off(self) -> None:
        self.app.config = run.load_config({**OPTIONS, "notify_on_failure": False})
        self.left = "100:30:00"
        with self.assertLogs(run.__name__, level="WARNING"):
            self.renew()
        self.assertEqual(self.notified, [])
        self.assertEqual(self.card()["badge"], {"text": "100 h left", "tone": "warn"})

    def test_paused_tv_is_not_reported(self) -> None:
        paused = {**OPTIONS, "sessions": [{"name": "TV", "token": "placeholder-token", "enabled": False}]}
        self.app.config = run.load_config(paused)
        self.app.status["tv"] = run.SessionStatus(last_ok=True, remaining_hours=10.0, remaining_at=time.time())
        self.app._tick()
        self.assertEqual(self.notified, [])
        self.assertEqual(self.card()["badge"], {"text": "Paused", "tone": "off"})

    def test_the_notification_names_the_tv_and_the_threshold(self) -> None:
        title, message = run.notification_text(run.load_config(OPTIONS).sessions[0], "low_session")
        self.assertIn("running out", title)
        self.assertIn("'TV'", message)
        self.assertIn("168 hours", message)


if __name__ == "__main__":
    unittest.main()
