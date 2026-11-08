"""Tests for the Ranhill SAJ client: form fields, page parsing and redirects (no network)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import ranhill

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACCOUNT = "12345678A1234567"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FormTest(unittest.TestCase):
    def test_fields_skip_digit_9_and_carry_the_letter_in_digit_10(self) -> None:
        fields = dict(ranhill.form_fields(ACCOUNT, "KEY"))
        self.assertEqual(fields["form_key"], "KEY")
        self.assertEqual("".join(fields[f"digit-{i}"] for i in range(1, 9)), "12345678")
        self.assertEqual(fields["digit-10"], "A")
        self.assertEqual("".join(fields[f"digit-{i}"] for i in range(11, 18)), "1234567")
        self.assertNotIn("digit-9", fields)
        self.assertEqual(len(fields), 17)

    def test_fields_reject_bad_numbers(self) -> None:
        for bad in ("1234567801234567", "12345678A123456", "A2345678A1234567"):
            with self.assertRaises(ValueError):
                ranhill.form_fields(bad, "KEY")

    def test_form_key_from_search_page(self) -> None:
        self.assertEqual(ranhill.extract_form_key(fixture("search.html")), "TESTFORMKEY12345")

    def test_missing_form_key_is_a_layout_change(self) -> None:
        with self.assertRaises(ranhill.ParseError):
            ranhill.extract_form_key("<html><body>maintenance</body></html>")


class AmountTest(unittest.TestCase):
    def test_formats(self) -> None:
        cases = {
            "RM 1,234.56": 1234.56,
            "RM12.34": 12.34,
            "12.3": 12.30,
            "0.00": 0.0,
            "RM 85": 85.0,
            "RM 1,000,000.00": 1_000_000.0,
            "RM -12.30": -12.30,
            "-12.30": -12.30,
            "RM (12.30)": -12.30,
            "12.30 CR": -12.30,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ranhill.parse_amount(text), expected)

    def test_zero_is_never_negative(self) -> None:
        self.assertEqual(str(ranhill.parse_amount("-0.00")), "0.0")

    def test_no_number(self) -> None:
        with self.assertRaises(ValueError):
            ranhill.parse_amount("RM")


class VerificationTest(unittest.TestCase):
    def test_amount_and_holder(self) -> None:
        bill = ranhill.parse_verification(fixture("verification_ok.html"))
        self.assertEqual(bill.amount_due, 1234.56)
        self.assertEqual(bill.amount_text, "RM 1,234.56")
        self.assertEqual(bill.holder, "AHMAD BIN ALI")

    def test_minimum_payment_text_is_not_the_amount(self) -> None:
        page = fixture("verification_ok.html").replace("RM 1,234.56", "RM 0.00")
        self.assertIn("RM10.00", page)
        self.assertEqual(ranhill.parse_verification(page).amount_due, 0.0)

    def test_empty_template_means_not_found(self) -> None:
        with self.assertRaises(ranhill.AccountNotFoundError):
            ranhill.parse_verification(fixture("verification_empty.html"))

    def test_search_page_means_not_found(self) -> None:
        with self.assertRaises(ranhill.AccountNotFoundError):
            ranhill.parse_verification(fixture("search.html"))

    def test_unknown_page_is_a_layout_change_and_keeps_the_page(self) -> None:
        with self.assertRaises(ranhill.ParseError) as ctx:
            ranhill.parse_verification("<html><body>Something else</body></html>")
        self.assertIn("Something else", ctx.exception.page)

    def test_renamed_label_is_a_layout_change(self) -> None:
        page = fixture("verification_ok.html").replace("Amount Due", "Jumlah")
        with self.assertRaises(ranhill.ParseError):
            ranhill.parse_verification(page)

    def test_garbage_amount_is_a_layout_change(self) -> None:
        page = fixture("verification_ok.html").replace("RM 1,234.56", "See bill")
        with self.assertRaises(ranhill.ParseError):
            ranhill.parse_verification(page)


class FakeClient(ranhill.SajClient):
    """SajClient whose HTTP layer replays canned replies."""

    def __init__(self, replies: list[ranhill._Reply]) -> None:
        super().__init__()
        self.replies = replies
        self.calls: list[tuple[str, list[tuple[str, str]] | None, str]] = []

    def _request(self, path: str, data: list[tuple[str, str]] | None = None, referer: str = "") -> ranhill._Reply:
        self.calls.append((path, data, referer))
        return self.replies.pop(0)


def search_reply() -> ranhill._Reply:
    return ranhill._Reply(200, "", fixture("search.html"))


class LookupTest(unittest.TestCase):
    def test_found(self) -> None:
        client = FakeClient([search_reply(), ranhill._Reply(200, "", fixture("verification_ok.html"))])
        self.assertEqual(client.lookup(ACCOUNT).amount_due, 1234.56)
        path, data, referer = client.calls[1]
        self.assertEqual(path, ranhill.VERIFICATION_PATH)
        self.assertEqual(dict(data or [])["form_key"], "TESTFORMKEY12345")
        self.assertTrue(referer.endswith(ranhill.SEARCH_PATH))
        self.assertIn("form_key", {c.name for c in client.jar})

    def test_redirect_to_search_is_not_found(self) -> None:
        location = "https://www.ranhillsaj.com.my/expresspayment/checkout/search/"
        client = FakeClient([search_reply(), ranhill._Reply(302, location, fixture("verification_empty.html"))])
        with self.assertRaises(ranhill.AccountNotFoundError):
            client.lookup(ACCOUNT)

    def test_redirect_to_verification_is_followed(self) -> None:
        client = FakeClient(
            [
                search_reply(),
                ranhill._Reply(302, "/expresspayment/checkout/verification/", ""),
                ranhill._Reply(200, "", fixture("verification_ok.html")),
            ]
        )
        self.assertEqual(client.lookup(ACCOUNT).amount_due, 1234.56)
        self.assertIsNone(client.calls[2][1])  # followed with a GET

    def test_unexpected_redirect_is_a_layout_change(self) -> None:
        client = FakeClient([search_reply(), ranhill._Reply(302, "/customer/account/login/", "")])
        with self.assertRaises(ranhill.ParseError):
            client.lookup(ACCOUNT)

    def test_server_error_is_transient(self) -> None:
        client = FakeClient([ranhill._Reply(503, "", "")])
        with self.assertRaises(ranhill.SiteError):
            client.lookup(ACCOUNT)


if __name__ == "__main__":
    unittest.main()
