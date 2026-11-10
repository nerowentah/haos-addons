"""Tests for the daily usage report."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rootfs" / "app"))

from providers.tnb import report
from providers.tnb.meter import MYT, billing_period_start
from providers.tnb.mytnb import EnergyPoint


def hourly(day: date, kwh_per_hour: float, hours: int = 24) -> list[EnergyPoint]:
    start = datetime(day.year, day.month, day.day, tzinfo=MYT)
    return [EnergyPoint(start=start + timedelta(hours=h), value=kwh_per_hour) for h in range(hours)]


def daily_cost(day: date, rm: float) -> EnergyPoint:
    return EnergyPoint(start=datetime(day.year, day.month, day.day, tzinfo=MYT), value=rm)


def cycle_from_23rd(day: date) -> date:
    return billing_period_start(day, 23)


class DailyReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.d1, self.d2, self.d3 = date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 7)
        self.usage = hourly(self.d1, 0.25) + hourly(self.d2, 0.5) + hourly(self.d3, 0.5, hours=10)  # d3 partial
        self.cost = [daily_cost(self.d1, 2.0), daily_cost(self.d2, 4.0)]

    def test_reports_newest_complete_day_with_trend_and_cycle(self) -> None:
        rep = report.build_report(self.usage, self.cost, cycle_from_23rd, None)
        assert rep is not None
        self.assertEqual(rep.day, self.d2)  # d3 is still partial
        self.assertAlmostEqual(rep.usage_kwh, 12.0)
        self.assertEqual(rep.cost_rm, 4.0)
        self.assertEqual(rep.cycle_start, date(2026, 10, 23))
        self.assertAlmostEqual(rep.cycle_kwh, 18.0)  # d1 + d2, not the partial d3
        message = rep.message("Home")
        self.assertIn("Home: 12.00 kWh, RM 4.00 (▲100% vs the day before)", message)
        self.assertIn("Cycle since 23 Oct: 18.00 kWh, RM 6.00.", message)
        self.assertEqual(rep.title, "TNB usage Fri 06 Nov")

    def test_once_per_day(self) -> None:
        self.assertIsNone(report.build_report(self.usage, self.cost, cycle_from_23rd, self.d2))
        self.assertIsNotNone(report.build_report(self.usage, self.cost, cycle_from_23rd, self.d1))

    def test_cycle_of_the_reported_day_not_today(self) -> None:
        d = date(2026, 11, 22)  # last day of the cycle that started 23 Oct
        rep = report.build_report(hourly(d, 1.0), [], cycle_from_23rd, None)
        assert rep is not None
        self.assertEqual(rep.cycle_start, date(2026, 10, 23))
        self.assertIsNone(rep.cost_rm)
        self.assertNotIn("RM 0", rep.message("Home").split(".")[0])

    def test_nothing_complete_yet(self) -> None:
        self.assertIsNone(report.build_report(hourly(self.d1, 1.0, hours=12), [], cycle_from_23rd, None))
        self.assertIsNone(report.build_report([], [], cycle_from_23rd, None))

    def test_flat_and_falling_trend(self) -> None:
        flat = report.build_report(hourly(self.d1, 0.5) + hourly(self.d2, 0.5), [], cycle_from_23rd, None)
        assert flat is not None
        self.assertIn("same as the day before", flat.message("Home"))
        down = report.build_report(hourly(self.d1, 1.0) + hourly(self.d2, 0.5), [], cycle_from_23rd, None)
        assert down is not None
        self.assertIn("▼50%", down.message("Home"))


if __name__ == "__main__":
    unittest.main()
