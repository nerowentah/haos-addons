"""Tests for the TNB tariff calculator."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import tariff


class GeneralTariffTest(unittest.TestCase):
    """Hand-computed general (non-ToU) bills."""

    def test_200_kwh(self) -> None:
        bill = tariff.calculate(200, 0)
        # 200 x (0.2703 + 0.0455 + 0.1285) - 200 x 0.25 EEI; no retail, KWTBB or SST
        self.assertAlmostEqual(bill.total, 38.86, places=2)
        self.assertEqual(bill.retail, 0)
        self.assertEqual(bill.kwtbb, 0)

    def test_600_kwh_has_kwtbb_but_no_sst_or_retail(self) -> None:
        bill = tariff.calculate(600, 0, afa_rate=0.05)
        self.assertEqual(bill.afa, 0)  # AFA only above 600 kWh
        self.assertEqual(bill.sst, 0)
        self.assertEqual(bill.retail, 0)
        self.assertAlmostEqual(bill.total, 215.98, places=2)

    def test_601_kwh_adds_retail_and_sst_on_excess(self) -> None:
        bill = tariff.calculate(601, 0)
        self.assertEqual(bill.retail, tariff.RETAIL_CHARGE)
        self.assertAlmostEqual(bill.sst, 0.8295, places=3)
        self.assertAlmostEqual(bill.total, 236.33, places=2)

    def test_1000_and_1001_kwh_eei_boundary(self) -> None:
        self.assertAlmostEqual(tariff.eei_rate(1000), 0.005)
        self.assertEqual(tariff.eei_rate(1001), 0)

    def test_1500_vs_1600_kwh_tier_applies_to_whole_bill(self) -> None:
        low = tariff.calculate(1500, 0)
        high = tariff.calculate(1600, 0)
        self.assertEqual(low.tier, 1)
        self.assertEqual(high.tier, 2)
        self.assertAlmostEqual(low.energy, 1500 * 0.2703)
        self.assertAlmostEqual(high.energy, 1600 * 0.3703)
        self.assertAlmostEqual(high.total, 939.16, places=2)

    def test_afa_applies_above_600(self) -> None:
        bill = tariff.calculate(700, 0, afa_rate=-0.0047)
        self.assertAlmostEqual(bill.afa, 700 * -0.0047)


class TouTariffTest(unittest.TestCase):
    """Time-of-Use specifics."""

    def test_peak_and_offpeak_rates(self) -> None:
        bill = tariff.calculate(100, 300, tariff=tariff.TARIFF_TOU)
        self.assertAlmostEqual(bill.energy, 100 * 0.2852 + 300 * 0.2443)

    def test_tier_two_rates(self) -> None:
        bill = tariff.calculate(800, 800, tariff=tariff.TARIFF_TOU)
        self.assertAlmostEqual(bill.energy, 800 * 0.3852 + 800 * 0.3443)

    def test_marginal_rate_peak_higher(self) -> None:
        peak = tariff.marginal_rate(tariff.TARIFF_TOU, 400, peak=True)
        offpeak = tariff.marginal_rate(tariff.TARIFF_TOU, 400, peak=False)
        self.assertGreater(peak, offpeak)


class NemTest(unittest.TestCase):
    """Export credit capping and carry-forward."""

    def test_partial_credit(self) -> None:
        bill = tariff.calculate(400, 0, 100)
        rate = 0.2703 + 0.0455 + 0.1285
        self.assertAlmostEqual(bill.nem_credit, -100 * rate)
        self.assertEqual(bill.nem_excess_kwh, 0)

    def test_credit_capped_and_excess_carried(self) -> None:
        bill = tariff.calculate(100, 0, 300)
        self.assertAlmostEqual(bill.nem_credit, -(bill.energy + bill.capacity + bill.network))
        self.assertAlmostEqual(bill.nem_excess_kwh, 200, places=6)
        self.assertGreaterEqual(bill.total, 0)

    def test_carry_counts_as_export(self) -> None:
        with_carry = tariff.calculate(400, 0, 0, nem_carry_kwh=50)
        with_export = tariff.calculate(400, 0, 50)
        self.assertAlmostEqual(with_carry.total, with_export.total)


if __name__ == "__main__":
    unittest.main()
