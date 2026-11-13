"""Tests for certs.py: parsing imports, chain order, names and issuing from an imported CA."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import certs
from cryptography import x509
from helpers import NOW, make_ca, make_leaf, pem


class ParseTest(unittest.TestCase):
    def test_certificate_and_key_in_any_order(self) -> None:
        ca = make_ca()
        leaf, key = make_leaf(["home.example.com"], ca)
        bundle = certs.parse_bundle(pem(key) + "\n" + pem(ca[0], leaf))
        self.assertEqual(len(bundle.certs), 2)
        self.assertIsNotNone(bundle.key)
        chain = certs.order_chain(bundle.certs, bundle.key)
        self.assertEqual(chain[0], leaf)
        self.assertEqual(chain[1], ca[0])

    def test_no_pem_data(self) -> None:
        with self.assertRaises(certs.CertError):
            certs.parse_bundle("hello")

    def test_two_keys_are_refused(self) -> None:
        ca = make_ca()
        _leaf, key = make_leaf(["a.example.com"], ca)
        with self.assertRaisesRegex(certs.CertError, "more than one private key"):
            certs.parse_bundle(pem(key, ca[1]))

    def test_encrypted_key_is_explained(self) -> None:
        text = "-----BEGIN ENCRYPTED PRIVATE KEY-----\nAAAA\n-----END ENCRYPTED PRIVATE KEY-----\n"
        with self.assertRaisesRegex(certs.CertError, "password-protected"):
            certs.parse_bundle(text)

    def test_damaged_certificate(self) -> None:
        with self.assertRaisesRegex(certs.CertError, "damaged"):
            certs.parse_bundle("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")

    def test_key_of_another_certificate(self) -> None:
        ca = make_ca()
        leaf, _key = make_leaf(["a.example.com"], ca)
        _other, other_key = make_leaf(["b.example.com"], ca)
        bundle = certs.parse_bundle(pem(leaf, other_key))
        with self.assertRaisesRegex(certs.CertError, "does not belong"):
            certs.order_chain(bundle.certs, bundle.key)

    def test_unrelated_certificate_in_the_chain(self) -> None:
        ca = make_ca()
        other_ca = make_ca("Other CA")
        intermediate, _ = make_leaf(["x.example.com"], other_ca)
        leaf, key = make_leaf(["a.example.com"], ca)
        with self.assertRaisesRegex(certs.CertError, "do not belong"):
            certs.order_chain([leaf, intermediate], key)


class ServerCertTest(unittest.TestCase):
    def test_expired(self) -> None:
        ca = make_ca()
        leaf, _key = make_leaf(["a.example.com"], ca, days=10, start_days=-30)
        with self.assertRaisesRegex(certs.CertError, "expired"):
            certs.check_server_cert([leaf])

    def test_ca_is_not_a_server_certificate(self) -> None:
        ca = make_ca()
        with self.assertRaisesRegex(certs.CertError, "CA certificate"):
            certs.check_server_cert([ca[0]])

    def test_valid(self) -> None:
        ca = make_ca()
        leaf, _key = make_leaf(["a.example.com"], ca)
        certs.check_server_cert([leaf])


class NamesTest(unittest.TestCase):
    def test_parse_names(self) -> None:
        self.assertEqual(
            certs.parse_names(
                "Home.Example.com, https://x.example.com/path 192.168.1.10 *.example.com home.example.com"
            ),
            ["home.example.com", "x.example.com", "192.168.1.10", "*.example.com"],
        )

    def test_bad_names(self) -> None:
        for text in ("", "under_score.example.com", "a..b"):
            with self.subTest(text=text), self.assertRaises(certs.CertError):
                certs.parse_names(text)

    def test_public_names(self) -> None:
        self.assertEqual(certs.public_names(["home.example.com"]), ["home.example.com"])
        for name in ("homeassistant.local", "192.168.1.10", "localhost"):
            with self.subTest(name=name), self.assertRaises(certs.CertError):
                certs.public_names([name])

    def test_covers(self) -> None:
        self.assertTrue(certs.covers(["*.example.com"], "home.example.com"))
        self.assertFalse(certs.covers(["*.example.com"], "a.b.example.com"))
        self.assertFalse(certs.covers(["*.example.com"], "example.com"))
        self.assertTrue(certs.covers(["Home.Example.com"], "home.example.com."))


class IssueTest(unittest.TestCase):
    def test_issue_covers_dns_and_ip_names(self) -> None:
        ca = make_ca()
        leaf, key = certs.issue(ca[0], ca[1], ["homeassistant.local", "192.168.1.10"])
        self.assertTrue(certs.same_key(leaf, key))
        self.assertTrue(certs.signed_by(leaf, ca[0]))
        self.assertEqual(certs.names(leaf), ["homeassistant.local", "192.168.1.10"])
        self.assertFalse(certs.is_ca(leaf))
        eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        self.assertIn(x509.oid.ExtendedKeyUsageOID.SERVER_AUTH, list(eku))
        self.assertLessEqual((leaf.not_valid_after_utc - NOW).days, certs.MAX_ISSUED_DAYS)

    def test_validity_never_outlives_the_ca(self) -> None:
        ca = make_ca(days=30)
        leaf, _key = certs.issue(ca[0], ca[1], ["a.local"])
        self.assertLessEqual(leaf.not_valid_after_utc, ca[0].not_valid_after_utc)

    def test_a_server_certificate_cannot_issue(self) -> None:
        ca = make_ca()
        leaf, key = make_leaf(["a.example.com"], ca)
        with self.assertRaisesRegex(certs.CertError, "not a CA"):
            certs.issue(leaf, key, ["b.local"])

    def test_wrong_key_for_the_ca(self) -> None:
        ca = make_ca()
        other = make_ca("Other")
        with self.assertRaisesRegex(certs.CertError, "does not belong"):
            certs.issue(ca[0], other[1], ["b.local"])

    def test_csr(self) -> None:
        key = certs.new_key()
        csr = x509.load_pem_x509_csr(certs.csr_pem(key, ["home.example.com", "*.example.com"]).encode())
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(san.get_values_for_type(x509.DNSName), ["home.example.com", "*.example.com"])

    def test_info_days_left(self) -> None:
        ca = make_ca()
        leaf, _key = make_leaf(["a.example.com"], ca, days=90)
        summary = certs.info(leaf)
        self.assertIn(summary.days_left(), (88, 89, 90))
        self.assertEqual(summary.as_dict()["names"], ["a.example.com"])
        self.assertTrue(summary.not_after > NOW + dt.timedelta(days=80))


if __name__ == "__main__":
    unittest.main()
