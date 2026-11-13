"""Tests for origin_tls.plan(): where the tunnel sends traffic and how it checks the certificate."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import cloudflared as cf
import origin_tls as otls

HTTP = {"port": 8123, "ssl": False}
HTTPS = {"port": 8123, "ssl": True}


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bundle = Path(tmp.name) / "ca-bundle.pem"
        self.bundle.write_text("-----BEGIN CERTIFICATE-----\n", encoding="ascii")
        self.status = Path(tmp.name) / "status.json"

    def manager(
        self, *, proxy: bool = False, ha_https: bool = False, names: list[str] | None = None
    ) -> dict[str, object]:
        names = names or ["home.example.com"]
        return {
            "version": 1,
            "trust_bundle": str(self.bundle),
            "default": {"id": "a", "names": names},
            "certificates": [{"id": "a", "names": names}, {"id": "b", "names": ["tunnel.example.com"]}],
            "proxy": {"enabled": proxy, "host": "a1b2c3d4-ssl-cert-manager", "port": 8443},
            "ha_https": ha_https,
        }

    def test_without_the_manager_nothing_changes(self) -> None:
        self.assertEqual(otls.plan("auto", HTTP, None, "home.example.com"), otls.Origin("http://homeassistant:8123"))
        https = otls.plan("auto", HTTPS, None, "home.example.com")
        self.assertTrue(https.no_tls_verify)  # as before 0.3.0
        self.assertEqual(https.request(), {"noTLSVerify": True})

    def test_proxy_is_checked_for_the_public_name(self) -> None:
        origin = otls.plan("auto", HTTP, self.manager(proxy=True), "tunnel.example.com")
        self.assertEqual(origin.service, "https://a1b2c3d4-ssl-cert-manager:8443")
        self.assertEqual(origin.request(), {"caPool": str(self.bundle), "originServerName": "tunnel.example.com"})
        self.assertIn("certificate checked for tunnel.example.com", origin.describe())

    def test_proxy_uses_a_covered_name_when_the_public_one_is_not_on_a_certificate(self) -> None:
        origin = otls.plan("auto", HTTP, self.manager(proxy=True), "other.example.org")
        self.assertEqual(origin.server_name, "home.example.com")
        wildcard = otls.plan(
            "auto", HTTP, {**self.manager(proxy=True), "certificates": [{"names": ["*.example.com"]}]}, "x.org"
        )
        self.assertEqual(wildcard.server_name, "origin.example.com")

    def test_home_assistant_https_with_the_managers_certificate(self) -> None:
        origin = otls.plan("auto", HTTPS, self.manager(ha_https=True), "home.example.com")
        self.assertEqual(origin.service, "https://homeassistant:8123")
        self.assertFalse(origin.no_tls_verify)
        self.assertEqual(origin.ca_pool, str(self.bundle))
        self.assertEqual(origin.server_name, "home.example.com")

    def test_verify_never_skips_the_check(self) -> None:
        origin = otls.plan("verify", HTTPS, None, "home.example.com")
        self.assertFalse(origin.no_tls_verify)
        self.assertEqual(origin.server_name, "home.example.com")

    def test_skip_is_the_old_behaviour(self) -> None:
        origin = otls.plan("skip", HTTPS, self.manager(proxy=True, ha_https=True), "home.example.com")
        self.assertEqual(origin, otls.Origin("https://homeassistant:8123", no_tls_verify=True, note=origin.note))

    def test_a_missing_bundle_file_means_no_proxy(self) -> None:
        self.bundle.unlink()
        origin = otls.plan("auto", HTTP, self.manager(proxy=True), "home.example.com")
        self.assertEqual(origin.service, "http://homeassistant:8123")

    def test_read_manager(self) -> None:
        self.assertIsNone(otls.read_manager(self.status))
        self.status.write_text(json.dumps({"version": 2}), encoding="utf-8")
        self.assertIsNone(otls.read_manager(self.status))
        self.status.write_text(json.dumps(self.manager()), encoding="utf-8")
        self.assertEqual(otls.read_manager(self.status)["version"], 1)  # type: ignore[index]


class OriginErrorTest(unittest.TestCase):
    def test_reason_from_a_cloudflared_line(self) -> None:
        line = (
            '2026-11-13T01:02:03Z ERR  error="Unable to reach the origin service. The service may be down or it may not '
            "be responding to traffic from cloudflared: tls: failed to verify certificate: x509: certificate is valid "
            'for home.example.com, not homeassistant" connIndex=0 event=1 ingressRule=0 originService=https://homeassistant:8123'
        )
        self.assertEqual(
            cf.origin_tls_reason(line),
            "tls: failed to verify certificate: x509: certificate is valid for home.example.com, not homeassistant",
        )


if __name__ == "__main__":
    unittest.main()
