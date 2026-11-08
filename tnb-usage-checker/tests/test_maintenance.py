"""Tests for maintenance windows, detection and back-off (shared module) and the TNB wiring."""

from __future__ import annotations

import random
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import maintenance
import mytnb
from meter import MYT


def at(text: str) -> datetime:
    """'2026-11-08 03:00' in Malaysia time (8 Nov 2026 is a Sunday)."""

    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=MYT)


class WindowTest(unittest.TestCase):
    def test_daily_default(self) -> None:
        windows = maintenance.parse_windows(None)
        self.assertEqual(maintenance.describe(windows), "daily 00:00-06:00")
        self.assertIsNotNone(maintenance.active_window(windows, at("2026-11-08 00:00")))
        self.assertIsNotNone(maintenance.active_window(windows, at("2026-11-08 05:59")))
        self.assertIsNone(maintenance.active_window(windows, at("2026-11-08 06:00")))
        self.assertEqual(maintenance.window_end(windows, at("2026-11-08 03:00")), at("2026-11-08 06:00"))
        self.assertIsNone(maintenance.window_end(windows, at("2026-11-08 12:00")))

    def test_empty_list_means_none(self) -> None:
        self.assertEqual(maintenance.parse_windows([]), [])
        self.assertEqual(maintenance.describe([]), "none")

    def test_crossing_midnight_belongs_to_the_start_day(self) -> None:
        (window,) = maintenance.parse_windows(["fri 23:00-02:00"])
        self.assertTrue(window.covers(at("2026-11-06 23:30")))  # Friday night
        self.assertTrue(window.covers(at("2026-11-07 01:59")))  # Saturday early morning
        self.assertFalse(window.covers(at("2026-11-07 23:30")))  # Saturday night
        self.assertEqual(window.end_after(at("2026-11-06 23:30")), at("2026-11-07 02:00"))
        self.assertEqual(window.end_after(at("2026-11-07 01:00")), at("2026-11-07 02:00"))

    def test_day_groups_ranges_and_lists(self) -> None:
        weekdays, weekend, listed, wrap, whole = maintenance.parse_windows(
            [
                "mon-fri 01:00-02:00",
                "weekends 01:00-02:00",
                "mon,wed 01:00-02:00",
                "fri-mon 01:00-02:00",
                "sun 00:00-24:00",
            ]
        )
        sunday, monday = at("2026-11-08 01:30"), at("2026-11-09 01:30")
        self.assertFalse(weekdays.covers(sunday))
        self.assertTrue(weekdays.covers(monday))
        self.assertTrue(weekend.covers(sunday))
        self.assertTrue(listed.covers(monday))
        self.assertTrue(wrap.covers(sunday) and wrap.covers(monday))
        self.assertTrue(whole.covers(at("2026-11-08 23:59")))
        self.assertEqual(whole.end_after(at("2026-11-08 10:00")), at("2026-11-09 00:00"))

    def test_back_to_back_windows_chain(self) -> None:
        windows = maintenance.parse_windows(["daily 00:00-06:00", "sun 06:00-08:00"])
        self.assertEqual(maintenance.window_end(windows, at("2026-11-08 05:00")), at("2026-11-08 08:00"))
        self.assertEqual(maintenance.window_end(windows, at("2026-11-09 05:00")), at("2026-11-09 06:00"))

    def test_bad_lines_explain_the_format(self) -> None:
        for line in (
            "nightly 00:00-06:00",
            "daily 25:00-06:00",
            "daily 6-8",
            "daily 01:00-01:00",
            "mon-xyz 01:00-02:00",
        ):
            with self.subTest(line=line), self.assertRaises(maintenance.MaintenanceConfigError):
                maintenance.parse_window(line)


class DetectionTest(unittest.TestCase):
    def test_detects_503_and_short_maintenance_pages(self) -> None:
        self.assertTrue(maintenance.looks_like_maintenance(503))
        self.assertTrue(maintenance.looks_like_maintenance(200, "<h1>System Maintenance</h1>"))
        self.assertTrue(maintenance.looks_like_maintenance(200, "", "myTNB - Under Maintenance"))
        self.assertTrue(maintenance.looks_like_maintenance(200, "Laman ini sedang dalam penyelenggaraan"))

    def test_ignores_normal_and_long_pages(self) -> None:
        self.assertFalse(maintenance.looks_like_maintenance(200, '{"result":"success"}'))
        self.assertFalse(maintenance.looks_like_maintenance(500, "Internal error"))
        long_page = "x" * (maintenance.SHORT_PAGE_CHARS + 1) + " scheduled maintenance notice in the footer"
        self.assertFalse(maintenance.looks_like_maintenance(200, long_page))


class BackoffTest(unittest.TestCase):
    def test_doubles_up_to_the_cap_and_resets(self) -> None:
        backoff = maintenance.Backoff(_random=random.Random(1))  # noqa: S311 - seeded jitter for a repeatable test
        delays = [backoff.next_delay() for _ in range(6)]
        for delay, expected in zip(delays, (1800, 3600, 7200, 14400, 21600, 21600), strict=True):
            self.assertAlmostEqual(delay, expected, delta=expected * 0.1 + 1)
        backoff.reset()
        self.assertAlmostEqual(backoff.next_delay(), 1800, delta=181)


class TnbWiringTest(unittest.TestCase):
    def test_option_default_and_validation(self) -> None:
        base = {"mytnb_username": "you@example.com", "mytnb_password": "x"}
        acct = [{"name": "Home", "tariff": "general", "billing_start_day": 1, "account_number": "123456789012"}]
        cfg = config.load_config({**base, "accounts": acct})
        self.assertEqual(maintenance.describe(cfg.maintenance_windows), "daily 00:00-06:00")
        cfg = config.load_config({**base, "accounts": acct, "maintenance_windows": []})
        self.assertEqual(cfg.maintenance_windows, [])
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config({**base, "accounts": acct, "maintenance_windows": ["sometimes"]})
        self.assertIn("maintenance_windows", str(ctx.exception))

    def test_portal_maintenance_page_raises_maintenance_error(self) -> None:
        session = mytnb.MyTnbSession("you@example.com", "pw", "123456789012")

        class Client:
            def open(self, url, body, headers):  # noqa: ARG002
                return mytnb._Response(503, "<title>Down</title>", "text/html", url)

        session._client = Client()  # type: ignore[assignment]
        with self.assertRaises(mytnb.MaintenanceError):
            session._send_raw(mytnb.LOGIN_URL, data={"Email": "e", "Password": "p"})
        self.assertTrue(issubclass(mytnb.MaintenanceError, mytnb.PortalError))
        self.assertFalse(issubclass(mytnb.MaintenanceError, mytnb.AuthError))

    def test_sync_is_deferred_inside_a_window(self) -> None:
        import main  # noqa: PLC0415 - heavy import only for this test

        class FakeApp:
            config = config.Config(
                portal_interval_hours=6,
                meter_poll_seconds=60,
                afa_rate=0.0,
                calendarific_api_key="",
                extra_holidays=[],
                notify_on_failure=True,
                accounts=[],
                maintenance_windows=maintenance.parse_windows(None),
            )
            maintenance_until = main.App.maintenance_until

        acct = config.load_config(
            {
                "mytnb_username": "you@example.com",
                "mytnb_password": "x",
                "accounts": [
                    {"name": "Home", "tariff": "general", "billing_start_day": 1, "account_number": "123456789012"}
                ],
            }
        ).accounts[0]
        runtime = main.AccountRuntime(account=acct)
        before = main.time.monotonic()
        deferred = main.App._defer_for_maintenance_window(FakeApp(), runtime, at("2026-11-08 05:00"))  # type: ignore[arg-type]
        self.assertTrue(deferred)
        wait = runtime.next_portal - before
        self.assertGreaterEqual(wait, timedelta(hours=1).total_seconds() + 59)  # until 06:00 + jitter
        self.assertLessEqual(wait, timedelta(hours=1).total_seconds() + 601)
        self.assertFalse(main.App._defer_for_maintenance_window(FakeApp(), runtime, at("2026-11-08 12:00")))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
