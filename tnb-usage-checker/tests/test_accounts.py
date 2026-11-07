"""Tests for one-login-many-accounts resolution, masking and setup mode."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import mytnb
import webui

LOGIN = {"mytnb_username": "you@example.com", "mytnb_password": "secret"}


def account(name: str, **extra: object) -> dict[str, object]:
    return {"name": name, "tariff": "general", "billing_start_day": 1, **extra}


class AccountResolutionTest(unittest.TestCase):
    def test_shared_login_many_accounts(self) -> None:
        cfg = config.load_config(
            {
                **LOGIN,
                "accounts": [
                    account("Home", account_number="1234 5678 9012"),
                    account("Shop", account_number="123456789013", import_entity="sensor.shop"),
                ],
            }
        )
        home, shop = cfg.accounts
        self.assertEqual((home.ca_no, shop.ca_no), ("123456789012", "123456789013"))
        self.assertEqual({home.mytnb_username, shop.mytnb_username}, {"you@example.com"})
        self.assertTrue(home.has_portal and shop.has_portal and shop.has_meter)
        self.assertEqual(home.smartmeter_path, "/AccountManagement/SmartMeter/Index/TRIL?caNo=123456789012")

    def test_per_account_login_overrides_shared(self) -> None:
        cfg = config.load_config(
            {
                **LOGIN,
                "accounts": [
                    account(
                        "Parents",
                        account_number="123456789014",
                        mytnb_username="parents@example.com",
                        mytnb_password="pw",
                    )
                ],
            }
        )
        self.assertEqual(cfg.accounts[0].mytnb_username, "parents@example.com")

    def test_legacy_smartmeter_url_still_works(self) -> None:
        url = "https://myaccount.mytnb.com.my/AccountManagement/SmartMeter/Index/TRIL?caNo=123456789015"
        cfg = config.load_config({**LOGIN, "accounts": [account("Old", smartmeter_url=url)]})
        self.assertEqual(cfg.accounts[0].ca_no, "123456789015")

    def test_login_without_account_number_explains_fix(self) -> None:
        # A login without an account number: a login on the account but no smart-meter link/number.
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config({"accounts": [account("Home", **LOGIN)]})
        self.assertIn("account_number", str(ctx.exception))
        self.assertIn("bill", str(ctx.exception))

    def test_account_number_without_login(self) -> None:
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_config({"accounts": [account("Home", account_number="123456789012")]})
        self.assertIn("mytnb_username", str(ctx.exception))

    def test_bad_inputs(self) -> None:
        cases = [
            {"accounts": [account("Home", account_number="12345")]},  # too short
            {"mytnb_username": "you@example.com", "accounts": [account("Home", account_number="123456789012")]},
            {
                "mytnb_username": "not-an-email",
                "mytnb_password": "x",
                "accounts": [account("A", account_number="123456789012")],
            },
            {"accounts": [account("Home", account_number="")]},  # empty default: nothing configured yet
            {"accounts": []},
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(config.ConfigError):
                config.load_config(raw)

    def test_session_built_from_account_number(self) -> None:
        session = mytnb.MyTnbSession("you@example.com", "pw", "123456789012")
        self.assertEqual(session.ca_no, "123456789012")
        self.assertTrue(session._smartmeter_path.endswith("caNo=123456789012"))


class MaskTest(unittest.TestCase):
    def test_masks(self) -> None:
        self.assertEqual(config.mask_number("123456789012"), "1234••••9012")
        self.assertEqual(config.mask_email("you@example.com"), "y••@example.com")
        self.assertEqual(config.mask_email(""), "••")


class SetupModeTest(unittest.TestCase):
    def test_waits_until_options_are_valid(self) -> None:
        answers = iter(
            [
                {"accounts": [account("Home", **LOGIN)]},
                {**LOGIN, "accounts": [account("Home", account_number="123456789012")]},
            ]
        )
        view = webui.SetupView("TNB", "first error", "fix it")
        result = webui.wait_for_valid_config(lambda: next(answers), config.load_config, view, threading.Event(), 0)
        self.assertIsNotNone(result)
        self.assertIn("account_number", view.error)  # the view shows the latest problem while waiting
        state = view.panel_state()
        self.assertEqual(state["sections"][0]["cards"][0]["title"], "Configuration needs attention")

    def test_stops_when_asked(self) -> None:
        stop = threading.Event()
        stop.set()
        view = webui.SetupView("TNB", "err", "fix")
        self.assertIsNone(webui.wait_for_valid_config(dict, config.load_config, view, stop, 0))

    def test_router_rejects_actions_in_setup(self) -> None:
        router = webui.PanelRouter(webui.SetupView("TNB", "err", "fix"))
        with self.assertRaises(webui.PanelError):
            router.on_toggle("x", True)
        self.assertEqual(webui.PanelRouter().get_state()["title"], "Starting")


if __name__ == "__main__":
    unittest.main()
