"""Tests for the nginx config, the certbot command and the Origin CA API calls (no processes, no network)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import acme
import certs
import origin_ca
import proxy
from helpers import make_ca, make_leaf, pem


class RenderTest(unittest.TestCase):
    def test_sni_servers_and_websocket_headers(self) -> None:
        default = proxy.Site(["home.example.com"], "/ssl/a/fullchain.pem", "/ssl/a/privkey.pem")
        other = proxy.Site(["home.example.com", "*.lan.example.com"], "/ssl/b/fullchain.pem", "/ssl/b/privkey.pem")
        dupe = proxy.Site(["home.example.com"], "/ssl/c/fullchain.pem", "/ssl/c/privkey.pem")
        text = proxy.render(default, [other, dupe], "http://homeassistant:8123")
        self.assertIn("listen 8443 ssl default_server;", text)
        self.assertEqual(text.count("server_name home.example.com;"), 1)
        self.assertIn("server_name *.lan.example.com;", text)  # the duplicate name stays with the default
        self.assertNotIn("/ssl/c/", text)  # nothing left to serve
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", text)
        self.assertIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", text)
        self.assertNotIn("proxy_ssl_verify", text)

    def test_https_upstream(self) -> None:
        site = proxy.Site(["a.example.com"], "/f", "/k")
        self.assertIn("proxy_ssl_verify off;", proxy.render(site, [], "https://homeassistant:8123"))
        self.assertEqual(proxy.upstream_for({"port": 8123, "ssl": True}), "https://homeassistant:8123")
        self.assertEqual(proxy.upstream_for({}), "http://homeassistant:8123")


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = None
        self.returncode: int | None = None
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return 0

    def kill(self) -> None:
        self.returncode = -9


class ProxyProcessTest(unittest.TestCase):
    def test_bad_config_is_never_used_and_reload_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spawned: list[FakeProcess] = []
            verdict = {"code": 1}

            def spawn(_argv: list[str]) -> FakeProcess:
                spawned.append(FakeProcess())
                return spawned[-1]

            p = proxy.Proxy(Path(tmp), spawn=spawn, run=lambda _a: (verdict["code"], "nginx: [emerg] bad\n"))  # type: ignore[arg-type]
            self.assertFalse(p.configure("bad"))
            self.assertEqual(p.error, "nginx: nginx: [emerg] bad")
            self.assertEqual(spawned, [])
            verdict["code"] = 0
            self.assertTrue(p.configure("good"))
            self.assertEqual(len(spawned), 1)
            self.assertTrue(p.configure("good"))  # unchanged: nothing happens
            self.assertEqual(spawned[0].signals, [])
            self.assertTrue(p.configure("better"))
            self.assertEqual(len(spawned[0].signals), 1)  # SIGHUP reload, same process
            p.stop()
            self.assertFalse(p.running)


class AcmeTest(unittest.TestCase):
    def test_token_only_in_a_private_file_and_removed_afterwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            seen: dict[str, object] = {}

            def run(argv: list[str]) -> tuple[int, str]:
                seen["argv"] = argv
                seen["credentials"] = (base / "cloudflare.ini").read_text()
                live = base / "config" / "live" / "home-example-com"
                live.mkdir(parents=True)
                (live / "fullchain.pem").write_text("x")
                (live / "privkey.pem").write_text("y")
                return 0, "Successfully received certificate."

            client = acme.Acme(base, run=run)
            issued = client.obtain(
                "home-example-com", ["home.example.com"], "token-0123456789-abcdefghij", "", staging=True
            )
            self.assertTrue(issued.fullchain.is_file())
            argv = seen["argv"]
            assert isinstance(argv, list)
            self.assertNotIn("token-0123456789-abcdefghij", " ".join(argv))
            self.assertIn("--staging", argv)
            self.assertIn("--register-unsafely-without-email", argv)
            self.assertEqual(argv[-2:], ["-d", "home.example.com"])
            self.assertIn("dns_cloudflare_api_token = token-0123456789-abcdefghij", str(seen["credentials"]))
            self.assertFalse((base / "cloudflare.ini").exists())

    def test_failure_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = "Saving debug log\nCertbot failed to authenticate some domains\nDetail: DNS problem: NXDOMAIN\n"
            client = acme.Acme(Path(tmp), run=lambda _a: (1, output))
            with self.assertRaisesRegex(acme.AcmeError, "NXDOMAIN"):
                client.obtain("x", ["home.example.com"], "token-0123456789-abcdefghij", "a@example.com", staging=False)
            with self.assertRaisesRegex(acme.AcmeError, "cloudflare_api_token"):
                client.obtain("x", ["home.example.com"], "", "", staging=False)


class OriginCaTest(unittest.TestCase):
    def test_create_sends_the_csr_and_returns_the_certificate(self) -> None:
        ca = make_ca("CloudFlare Origin SSL ECC Certificate Authority")
        leaf, _key = make_leaf(["home.example.com"], ca)
        calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

        def http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
            calls.append((method, url, headers, body))
            return 200, json.dumps({"success": True, "result": {"certificate": pem(leaf), "id": "123"}}).encode()

        client = origin_ca.OriginCa(http)
        csr = certs.csr_pem(certs.new_key(), ["home.example.com"])
        cert_pem, cert_id = client.create("token-0123456789-abcdefghij", ["home.example.com"], csr)
        self.assertEqual(cert_id, "123")
        self.assertIn("BEGIN CERTIFICATE", cert_pem)
        method, url, headers, body = calls[0]
        self.assertEqual((method, url), ("POST", "https://api.cloudflare.com/client/v4/certificates"))
        self.assertEqual(headers["Authorization"], "Bearer token-0123456789-abcdefghij")
        sent = json.loads(body or b"{}")
        self.assertEqual(sent["request_type"], "origin-ecc")
        self.assertEqual(sent["requested_validity"], 5475)

    def test_permission_error_names_the_permission(self) -> None:
        client = origin_ca.OriginCa(
            lambda *_a: (403, b'{"success":false,"errors":[{"code":10000,"message":"Authentication error"}]}')
        )
        with self.assertRaisesRegex(origin_ca.OriginCaError, "SSL and Certificates"):
            client.create("token-0123456789-abcdefghij", ["home.example.com"], "csr")

    def test_bundled_roots_are_cloudflare_origin_cas(self) -> None:
        bundle = certs.parse_bundle(origin_ca.roots_pem())
        self.assertEqual(len(bundle.certs), 2)
        for cert in bundle.certs:
            self.assertIn("CloudFlare Origin SSL", cert.subject.rfc4514_string())
            self.assertTrue(certs.is_ca(cert))


if __name__ == "__main__":
    unittest.main()
