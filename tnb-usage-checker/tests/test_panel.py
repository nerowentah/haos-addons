"""Tests for the TNB account cards (hero, cycle progress, daily chart)."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import panel
import report
from main import AccountRuntime

LOGIN = {"mytnb_username": "you@example.com", "mytnb_password": "secret"}
TODAY = date(2026, 11, 8)


def account(**extra: object) -> config.Account:
    entry = {"name": "Home", "tariff": "general", "billing_start_day": 23, **extra}
    return config.load_config({**LOGIN, "accounts": [entry]}).accounts[0]


class AccountCardTest(unittest.TestCase):
    def test_portal_card_has_hero_progress_chart_and_settings(self) -> None:
        acct = account(account_number="123456789012")
        runtime = AccountRuntime(
            account=acct,
            values={"portal_cost_cycle": 110.62, "portal_usage_cycle": 248.99, "portal_status": "ok"},
        )
        history = {date(2026, 11, 5): 9.72, date(2026, 11, 6): 10.89}
        card = panel._account_card(runtime, acct, history, TODAY)

        self.assertEqual(card["id"], "account:home")
        self.assertEqual([h["value"] for h in card["hero"]], ["RM 110.62", "248.99"])
        self.assertEqual(card["progress"]["label"], "Day 17 of 31")  # 23 Oct .. 23 Nov
        self.assertIn("15 days left", card["progress"]["caption"])
        self.assertAlmostEqual(card["progress"]["value"], 17 / 31, places=3)
        self.assertEqual([b["label"] for b in card["chart"]["bars"]], ["5 Nov", "6 Nov"])
        self.assertEqual(card["chart"]["caption"], "6 Nov: 10.89 kWh ▲12%")
        self.assertEqual(card["toggles_label"], "Settings")
        self.assertEqual(
            [t["id"] for t in card["toggles"]],
            ["account:home:enabled", "account:home:portal_enabled", "account:home:statistics_enabled"],
        )
        labels = [s["label"] for s in card["stats"]]
        self.assertIn("Account no.", labels)
        self.assertNotIn("myTNB cost (cycle)", labels)  # already the hero

    def test_sensor_account_leads_with_the_bill_estimate(self) -> None:
        acct = account(import_entity="sensor.grid_energy")
        runtime = AccountRuntime(account=acct, values={"bill_estimate": 87.5, "cycle_import": 301.2})
        card = panel._account_card(runtime, acct, {}, TODAY)
        self.assertEqual(card["hero"][0]["label"], "Bill estimate")
        self.assertEqual(card["hero"][0]["value"], "RM 87.50")
        self.assertIsNone(card["chart"])  # no myTNB data, no chart
        self.assertEqual(card["actions"], [])

    def test_no_history_no_chart(self) -> None:
        acct = account(account_number="123456789012")
        card = panel._account_card(AccountRuntime(account=acct, values={}), acct, {}, TODAY)
        self.assertIsNone(card["chart"])
        self.assertEqual(card["hero"][0]["value"], "-")


class CaptionTest(unittest.TestCase):
    def test_latest_day_caption(self) -> None:
        self.assertEqual(report.latest_day_caption({}), "")
        self.assertEqual(report.latest_day_caption({date(2026, 11, 6): 10.0}), "6 Nov: 10.00 kWh")
        self.assertEqual(
            report.latest_day_caption({date(2026, 11, 5): 10.0, date(2026, 11, 6): 5.0}), "6 Nov: 5.00 kWh ▼50%"
        )


if __name__ == "__main__":
    unittest.main()
