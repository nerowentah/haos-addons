"""Tests for the energy-sensor accumulator and helpers."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rootfs" / "app"))

from providers.tnb import config, meter, stats
from providers.tnb.mytnb import EnergyPoint, parse_points

MYT = meter.MYT
WEDNESDAY = datetime(2026, 11, 4, tzinfo=MYT)
SATURDAY = datetime(2026, 11, 7, tzinfo=MYT)


def no_holiday(_day: date) -> bool:
    return False


class PeakSplitTest(unittest.TestCase):
    """ToU peak window splitting."""

    def test_across_peak_start(self) -> None:
        share = meter.peak_fraction(WEDNESDAY.replace(hour=13), WEDNESDAY.replace(hour=15), no_holiday)
        self.assertAlmostEqual(share, 0.5)

    def test_across_peak_end(self) -> None:
        share = meter.peak_fraction(WEDNESDAY.replace(hour=21), WEDNESDAY.replace(hour=23), no_holiday)
        self.assertAlmostEqual(share, 0.5)

    def test_across_midnight_into_weekend(self) -> None:
        friday = WEDNESDAY + timedelta(days=2)
        share = meter.peak_fraction(friday.replace(hour=20), friday.replace(hour=20) + timedelta(hours=8), no_holiday)
        self.assertAlmostEqual(share, 2 / 8)

    def test_weekend_and_holiday_are_offpeak(self) -> None:
        self.assertEqual(meter.peak_fraction(SATURDAY.replace(hour=14), SATURDAY.replace(hour=16), no_holiday), 0)
        holiday = WEDNESDAY.date()
        share = meter.peak_fraction(WEDNESDAY.replace(hour=14), WEDNESDAY.replace(hour=16), lambda d: d == holiday)
        self.assertEqual(share, 0)


class CounterTest(unittest.TestCase):
    """Reset and spike guards."""

    def test_normal_reset_and_spike(self) -> None:
        counter = meter.Counter()
        self.assertEqual(counter.update(100.0, 0), 0)
        self.assertAlmostEqual(counter.update(101.0, 60), 1.0)
        self.assertEqual(counter.update(5.0, 120), 0)  # meter reset
        self.assertAlmostEqual(counter.update(6.0, 180), 1.0)
        self.assertEqual(counter.update(500.0, 240), 0)  # spike rejected, baseline kept
        self.assertAlmostEqual(counter.update(6.5, 300), 0.5)

    def test_three_spikes_rebaseline(self) -> None:
        counter = meter.Counter(last_value=0.0, last_ts=0.0)
        for i in range(3):
            counter.update(1000.0, 60.0 * (i + 1))
        self.assertEqual(counter.last_value, 1000.0)
        self.assertAlmostEqual(counter.update(1001.0, 300), 1.0)


class BillingCycleTest(unittest.TestCase):
    """Cycle boundaries and roll-over."""

    def test_period_start_clamps_short_months(self) -> None:
        self.assertEqual(meter.billing_period_start(date(2026, 2, 28), 31), date(2026, 2, 28))
        self.assertEqual(meter.billing_period_start(date(2026, 3, 15), 31), date(2026, 2, 28))
        self.assertEqual(meter.billing_period_start(date(2026, 1, 3), 5), date(2025, 12, 5))
        self.assertEqual(meter.next_period_start(date(2025, 12, 5), 5), date(2026, 1, 5))

    def test_ingest_and_roll_over(self) -> None:
        acc = meter.AccountMeter(billing_start_day=5)
        start = datetime(2026, 11, 3, 13, 0, tzinfo=MYT)  # Tuesday, cycle started 5 Oct
        acc.roll_over(start, "tou", 0.0)
        acc.ingest(start, 1000.0, None, no_holiday)
        end = start + timedelta(hours=2)
        acc.ingest(end, 1004.0, None, no_holiday)
        self.assertAlmostEqual(acc.peak_kwh, 2.0)
        self.assertAlmostEqual(acc.offpeak_kwh, 2.0)

        later = datetime(2026, 11, 5, 1, 0, tzinfo=MYT)
        acc.roll_over(later, "tou", 0.0)
        self.assertEqual(acc.period_start, "2026-11-05")
        self.assertEqual(len(acc.history), 1)
        self.assertAlmostEqual(float(acc.history[0]["import_kwh"]), 4.0)
        self.assertEqual(acc.peak_kwh, 0)

        restored = meter.AccountMeter.from_dict(acc.to_dict(), 5)
        self.assertEqual(restored.to_dict(), acc.to_dict())

    def test_prediction_not_below_usage(self) -> None:
        acc = meter.AccountMeter(billing_start_day=1, period_start="2026-11-01", peak_kwh=100, offpeak_kwh=100)
        bill = acc.predicted_bill(datetime(2026, 11, 11, tzinfo=MYT), "general", 0.0)
        self.assertIsNotNone(bill)
        self.assertAlmostEqual(bill.import_kwh, 600, places=3)  # 200 kWh in 10 days of a 30-day cycle


class ConfigTest(unittest.TestCase):
    """Option validation."""

    def test_accounts(self) -> None:
        cfg = config.load_config(
            {
                "accounts": [
                    {"name": "Home", "tariff": "tou", "billing_start_day": 3, "import_entity": "sensor.grid"},
                    {
                        "name": "Shop Lot 2",
                        "tariff": "general",
                        "billing_start_day": 1,
                        "mytnb_username": "a@b.c",
                        "mytnb_password": "x",
                        "smartmeter_url": "https://myaccount.mytnb.com.my/AccountManagement/SmartMeter/Index/TRIL?caNo=123",
                    },
                ]
            }
        )
        self.assertEqual([a.account_id for a in cfg.accounts], ["home", "shop_lot_2"])
        self.assertTrue(cfg.accounts[0].has_meter)
        self.assertTrue(cfg.accounts[1].has_portal)

    def test_rejects_duplicates_and_partial_portal(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_config(
                {"accounts": [{"name": "A", "import_entity": "sensor.x"}, {"name": "a", "import_entity": "sensor.y"}]}
            )
        with self.assertRaises(config.ConfigError):
            config.load_config({"accounts": [{"name": "A", "mytnb_username": "a@b.c"}]})
        with self.assertRaises(config.ConfigError):
            config.load_config({"accounts": [{"name": "A"}]})


class PortalParsingTest(unittest.TestCase):
    """Timeseries parsing and statistics rows."""

    def test_parse_and_rows(self) -> None:
        payload = {
            "data": {
                "timeseries": [
                    {
                        "data": [
                            {"datetime": "2026-11-01 00:30", "value": 0.4},
                            {"datetime": "2026-11-01 00:00", "value": 0.5},
                            {"datetime": "2026-11-01 01:00", "value": None},
                            {"datetime": "2026-11-01 02:00", "value": 1.0},
                        ]
                    }
                ]
            }
        }
        points = parse_points(payload)
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0].start.hour, 0)
        buckets = stats.hourly_buckets(points)
        rows = stats.build_rows(buckets, baseline=10.0)
        self.assertEqual([r["sum"] for r in rows], [10.9, 11.9])
        self.assertTrue(str(rows[0]["start"]).endswith("+00:00"))
        self.assertIsInstance(points[0], EnergyPoint)


if __name__ == "__main__":
    unittest.main()
