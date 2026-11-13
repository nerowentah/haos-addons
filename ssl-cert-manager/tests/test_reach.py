"""Tests for reach.py: the public URL check names its reason and bypasses a local DNS override (no network)."""

from __future__ import annotations

import socket
import ssl
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import reach

EDGE = "104.16.1.1"
LAN = "192.168.1.20"


def no_doh(_hostname: str) -> list[str]:
    raise AssertionError("public DNS must not be asked when the local answer is public")


class Fetcher:
    def __init__(self, outcome: object = 401) -> None:
        self.outcome = outcome
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, address: str, hostname: str, path: str) -> int:
        self.calls.append((address, hostname, path))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return int(self.outcome)  # type: ignore[call-overload]


class ProbeTest(unittest.TestCase):
    def test_a_public_answer_is_used_directly(self) -> None:
        fetch = Fetcher(401)
        result = reach.probe("home.example.com", resolve=lambda _h: [EDGE], doh=no_doh, fetch=fetch)
        self.assertTrue(result.ok)
        self.assertEqual(fetch.calls, [(EDGE, "home.example.com", "/api/")])
        self.assertEqual(result.note, "")

    def test_a_local_dns_override_is_bypassed_through_public_dns(self) -> None:
        fetch = Fetcher(401)
        result = reach.probe("home.example.com", resolve=lambda _h: [LAN], doh=lambda _h: [EDGE], fetch=fetch)
        self.assertTrue(result.ok)
        self.assertEqual(fetch.calls[0][0], EDGE)
        self.assertEqual(result.note, f"local DNS points to {LAN}; checked through public DNS")
        text, tone = reach.explain(result, lambda _code: ("Reachable", "ok"))
        self.assertEqual((text, tone), (f"Reachable (local DNS points to {LAN}; checked through public DNS)", "ok"))

    def test_only_a_private_address_and_no_public_one(self) -> None:
        fetch = Fetcher(ConnectionRefusedError(111, "Connection refused"))
        result = reach.probe("home.example.com", resolve=lambda _h: [LAN], doh=lambda _h: [], fetch=fetch)
        self.assertIsNone(result.code)
        self.assertEqual(
            result.reason, f"connection refused by {LAN} (local DNS override; the name has no public address)"
        )

    def test_a_name_that_resolves_nowhere(self) -> None:
        def fail(_h: str) -> list[str]:
            raise socket.gaierror(-2, "Name or service not known")

        def doh_down(_h: str) -> list[str]:
            raise OSError("network unreachable")

        result = reach.probe("home.example.com", resolve=fail, doh=doh_down, fetch=Fetcher())
        self.assertEqual(
            result.reason,
            "home.example.com does not resolve from Home Assistant or in public DNS (Name or service not known)",
        )
        text, tone = reach.explain(result, lambda _code: ("x", "ok"))
        self.assertTrue(text.startswith("No answer from the public hostname: home.example.com does not resolve"))
        self.assertEqual(tone, "warn")

    def test_a_certificate_for_another_name_explains_the_one_level_limit(self) -> None:
        err = ssl.SSLCertVerificationError(1, "certificate verify failed")
        err.verify_message = "Hostname mismatch, certificate is not valid for 'ha.home.example.com'."
        fetch = Fetcher(err)
        result = reach.probe("ha.home.example.com", resolve=lambda _h: [EDGE, "104.16.2.2"], doh=no_doh, fetch=fetch)
        self.assertTrue(result.reason.startswith("TLS: Hostname mismatch"))
        self.assertIn("covers one level", result.reason)
        self.assertEqual(len(fetch.calls), 1)  # a certificate problem is the same on every edge address

    def test_a_timeout_tries_a_second_address(self) -> None:
        fetch = Fetcher(TimeoutError("timed out"))
        result = reach.probe(
            "home.example.com", resolve=lambda _h: [EDGE, "104.16.2.2", "104.16.3.3"], doh=no_doh, fetch=fetch
        )
        self.assertEqual(len(fetch.calls), reach.MAX_TARGETS)
        self.assertEqual(result.reason, f"no answer within {reach.TIMEOUT_SECONDS} s from 104.16.2.2")

    def test_any_http_status_counts_as_an_answer(self) -> None:
        result = reach.probe("home.example.com", resolve=lambda _h: [EDGE], doh=no_doh, fetch=Fetcher(502))
        self.assertEqual((result.code, result.ok), (502, False))
        text, _tone = reach.explain(result, lambda code: (f"HTTP {code}", "error"))
        self.assertEqual(text, "HTTP 502")

    def test_ipv6_and_private_ranges(self) -> None:
        self.assertTrue(reach._routable("2606:4700::6810:101"))
        for address in ("10.0.0.5", "172.30.33.2", "127.0.0.1", "fe80::1", "100.64.0.1", "not-an-ip"):
            with self.subTest(address=address):
                self.assertFalse(reach._routable(address))


if __name__ == "__main__":
    unittest.main()
