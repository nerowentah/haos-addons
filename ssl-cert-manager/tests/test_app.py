"""Tests for the app: panel actions end to end with fake Supervisor, certbot, Cloudflare and nginx."""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import acme
import config
import files
import ha_config
import main
import origin_ca
import proxy
import reach
import store
import supervisor
import webui
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from helpers import NOW, make_ca, make_leaf, pem

TOKEN = "cf-token-0123456789abcdefghijklmnop"  # noqa: S105 - test value


class FakeSupervisor(supervisor.Supervisor):
    def __init__(self) -> None:
        super().__init__("token")
        self.stored: dict[str, object] = {"cloudflare_api_token": TOKEN, "local_proxy": False}
        self.sent: list[tuple[list[str], str, str]] = []
        self.targets: list[str] = ["mobile_app_phone"]
        self.restarted = 0
        self.tunnel_state: str | None = "started"

    def fetch_options(self) -> dict[str, object]:
        return copy.deepcopy(self.stored)

    def write_options(self, options: dict[str, object]) -> None:
        self.stored = copy.deepcopy(options)

    def discover_mobile_targets(self) -> list[str] | None:
        return list(self.targets)

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:  # noqa: ARG002
        self.sent.append((list(targets), title, message))

    def self_ip(self) -> str | None:
        return "172.30.33.7"

    def self_info(self) -> dict[str, object]:
        return {"slug": "a1b2c3d4_ssl_cert_manager", "hostname": "a1b2c3d4-ssl-cert-manager"}

    def addon_state(self, slug: str) -> str | None:
        self.asked = slug
        return self.tunnel_state

    def core_info(self) -> dict[str, object]:
        return {"port": 8123, "ssl": False}

    def check_core_config(self) -> tuple[bool, str]:
        return True, ""

    def restart_core(self) -> None:
        self.restarted += 1


class FakeAcme(acme.Acme):
    """Issues from a local CA instead of Let's Encrypt."""

    def __init__(self, base: Path) -> None:
        super().__init__(base)
        self.ca = make_ca("Fake LE")
        self.requests: list[tuple[str, list[str], bool]] = []
        self.fail: str | None = None

    def obtain(self, cert_id: str, hostnames: list[str], api_token: str, email: str, *, staging: bool) -> acme.Issued:  # noqa: ARG002
        self.requests.append((cert_id, hostnames, staging))
        if self.fail:
            raise acme.AcmeError(self.fail)
        leaf, key = make_leaf(hostnames, self.ca)
        issued = self.files(cert_id)
        issued.fullchain.parent.mkdir(parents=True, exist_ok=True)
        issued.fullchain.write_text(pem(leaf, self.ca[0]))
        issued.privkey.write_text(pem(key))
        return issued


class FakeOrigin(origin_ca.OriginCa):
    """Signs the CSR's key with a local stand-in for Cloudflare's Origin CA."""

    def __init__(self) -> None:
        super().__init__(lambda *_a: (500, b""))
        self.ca = make_ca("CloudFlare Origin SSL ECC Certificate Authority")
        self.revoked: list[str] = []

    def create(self, token: str, hostnames: list[str], csr: str) -> tuple[str, str]:  # noqa: ARG002
        request = x509.load_pem_x509_csr(csr.encode())
        ca_cert, ca_key = self.ca
        cert = (
            x509.CertificateBuilder()
            .subject_name(request.subject)
            .issuer_name(ca_cert.subject)
            .public_key(request.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - dt.timedelta(days=1))
            .not_valid_after(NOW + dt.timedelta(days=5475))
            .add_extension(
                request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value, critical=False
            )
            .sign(ca_key, hashes.SHA256())
        )
        return pem(cert), "origin-1"

    def revoke(self, token: str, certificate_id: str) -> None:  # noqa: ARG002
        self.revoked.append(certificate_id)


class FakeProxy(proxy.Proxy):
    def __init__(self) -> None:
        super().__init__(Path("/nonexistent"))
        self.texts: list[str] = []
        self.stopped = 0

    @property
    def running(self) -> bool:
        return bool(self.texts) and self._text is not None

    def configure(self, text: str) -> bool:
        if text != self._text:
            self.texts.append(text)
        self._text = text
        return True

    def stop(self) -> None:
        self.stopped += 1
        self._text = None

    def poll(self) -> None:
        return


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "ssl").mkdir()
        (base / "ha").mkdir()
        self.ha_path = base / "ha" / "configuration.yaml"
        self.ha_path.write_text("default_config:\n", encoding="utf-8")
        self.tunnel_file = base / "ssl" / "cloudflare-tunnel" / "status.json"
        self.sup = FakeSupervisor()
        self.acme = FakeAcme(base / "acme")
        self.origin = FakeOrigin()
        self.proxy = FakeProxy()
        self.probes: list[int | None] = []
        self.sleeps: list[float] = []
        self.app = main.App(
            config.load_config(self.sup.stored),
            self.sup,
            store.Store(base / "state.json"),
            copy.deepcopy(self.sup.stored),
            files=files.Files(base / "data", base / "ssl"),
            acme=self.acme,
            origin=self.origin,
            proxy=self.proxy,
            ha_path=self.ha_path,
            probe=lambda _host: reach.Result(self.probes.pop(0) if self.probes else 401),
            tunnel_status=self.tunnel_file,
            sleep=self.sleeps.append,
        )
        self.app._identify()
        self.app.discovered = ["mobile_app_phone"]
        self.ssl = base / "ssl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def act(self, action_id: str, args: dict[str, object] | None = None) -> str | None:
        reply = self.app.panel_action(action_id, args or {})
        self.drain()
        return reply

    def drain(self) -> None:
        while not self.app.jobs.empty():
            label, job = self.app.jobs.get()
            self.app.run_job(label, job)
        if self.app.need_sync:
            self.app.sync()
            while not self.app.jobs.empty():
                label, job = self.app.jobs.get()
                self.app.run_job(label, job)

    def state_text(self) -> str:
        return json.dumps(self.app.panel_state())

    # -- adding --------------------------------------------------------------------------
    def test_import_certificate_with_key_becomes_default_and_is_published(self) -> None:
        ca = make_ca()
        leaf, key = make_leaf(["home.example.com"], ca)
        self.act(
            "import_cert",
            {"files": [{"name": "cert.pem", "text": pem(leaf, ca[0])}, {"name": "key.pem", "text": pem(key)}]},
        )
        self.assertEqual(self.app.last_job, ("Imported the certificate for home.example.com", "ok"))
        self.assertEqual(self.app.default_id, "home-example-com")
        share = self.ssl / "ssl-cert-manager"
        self.assertTrue((share / "default" / "privkey.pem").is_file())
        self.assertTrue((self.ssl / "fullchain.pem").is_file())
        status = json.loads((share / "status.json").read_text())
        self.assertEqual(status["default"]["names"], ["home.example.com"])
        self.assertFalse(status["proxy"]["enabled"])
        self.assertEqual(status["proxy"]["host"], "a1b2c3d4-ssl-cert-manager")
        text = self.state_text()
        self.assertIn("home.example.com", text)
        self.assertNotIn("PRIVATE KEY", text)

    def test_import_without_key_is_refused(self) -> None:
        ca = make_ca()
        leaf, _key = make_leaf(["home.example.com"], ca)
        self.act("import_cert", {"files": [{"name": "cert.pem", "text": pem(leaf)}]})
        assert self.app.last_job is not None
        self.assertEqual(self.app.last_job[1], "error")
        self.assertIn("private key", self.app.last_job[0])
        self.assertEqual(self.app.certs, {})

    def test_upload_needs_files(self) -> None:
        with self.assertRaises(webui.PanelError):
            self.app.panel_action("import_cert", {})

    def test_ca_with_key_issues_local_certificates_and_renews_them(self) -> None:
        ca = make_ca("Home CA")
        self.act("import_ca", {"files": [{"name": "ca.pem", "text": pem(ca[0], ca[1])}]})
        ca_id = next(iter(self.app.cas))
        self.assertTrue(self.app.cas[ca_id]["can_issue"])
        self.act(f"issue:{ca_id}", {"text": "homeassistant.local, 192.168.1.10"})
        meta = self.app.certs["homeassistant-local"]
        self.assertEqual(meta["source"], main.SOURCE_ISSUED)
        self.assertEqual(meta["names"], ["homeassistant.local", "192.168.1.10"])
        bundle = (self.ssl / "ssl-cert-manager" / "trust" / "ca-bundle.pem").read_text()
        self.assertEqual(bundle.count("BEGIN CERTIFICATE"), 3)  # two Cloudflare roots + Home CA
        # Close to expiry: the loop renews it by itself.
        meta["info"]["not_after"] = (NOW + dt.timedelta(days=5)).isoformat()  # type: ignore[index]
        self.app.check_expiry()
        self.drain()
        self.assertGreater(
            dt.datetime.fromisoformat(self.app.certs["homeassistant-local"]["info"]["not_after"]),  # type: ignore[index]
            NOW + dt.timedelta(days=300),
        )

    def test_trust_only_ca_cannot_issue(self) -> None:
        ca = make_ca("Corp Root")
        self.act("import_ca", {"files": [{"name": "root.crt", "text": pem(ca[0])}]})
        ca_id = next(iter(self.app.cas))
        self.assertFalse(self.app.cas[ca_id]["can_issue"])
        with self.assertRaises(webui.PanelError):
            self.app.panel_action(f"issue:{ca_id}", {"text": "a.local"})
        self.act("import_ca", {"files": [{"name": "root.crt", "text": pem(ca[0])}]})
        self.assertEqual(len(self.app.cas), 1)  # the same CA twice is one entry

    def test_lets_encrypt_and_failed_renewal_notifies(self) -> None:
        self.act("letsencrypt", {"text": "home.example.com"})
        self.assertEqual(self.acme.requests, [("home-example-com", ["home.example.com"], False)])
        self.assertEqual(self.app.certs["home-example-com"]["source"], main.SOURCE_LE)
        self.acme.fail = "Let's Encrypt: DNS problem"
        self.act("renew:home-example-com")
        self.assertIn("DNS problem", str(self.app.certs["home-example-com"]["error"]))
        self.assertEqual(self.sup.sent[-1][1], "Certificate renewal failed")
        # A failed renewal waits before the loop tries again.
        self.app.certs["home-example-com"]["info"]["not_after"] = (NOW + dt.timedelta(days=3)).isoformat()  # type: ignore[index]
        self.app.check_expiry()
        self.assertTrue(self.app.jobs.empty())

    def test_lets_encrypt_refuses_local_names(self) -> None:
        with self.assertRaises(webui.PanelError):
            self.app.panel_action("letsencrypt", {"text": "homeassistant.local"})

    def test_origin_certificate_is_revoked_on_delete(self) -> None:
        self.act("origin", {"text": "home.example.com"})
        meta = self.app.certs["home-example-com"]
        self.assertEqual((meta["source"], meta["origin_id"]), (main.SOURCE_ORIGIN, "origin-1"))
        self.act("delete:home-example-com")
        self.assertEqual(self.origin.revoked, ["origin-1"])
        self.assertEqual(self.app.certs, {})
        self.assertFalse((self.ssl / "ssl-cert-manager" / "home-example-com").exists())

    def test_imported_certificate_expiring_notifies_once(self) -> None:
        ca = make_ca()
        leaf, key = make_leaf(["home.example.com"], ca, days=10)
        self.act("import_cert", {"files": [{"name": "both.pem", "text": pem(leaf, key)}]})
        self.app.check_expiry()
        self.app.check_expiry()
        titles = [title for _t, title, _m in self.sup.sent]
        self.assertEqual(titles.count("Certificate expiring"), 1)

    # -- local access ----------------------------------------------------------------------
    def test_proxy_serves_every_certificate_and_trusts_itself_in_home_assistant(self) -> None:
        self.act("letsencrypt", {"text": "home.example.com"})
        self.act("origin", {"text": "tunnel.example.com"})
        self.app.panel_toggle("local_proxy", True)
        self.app._tick()
        self.drain()
        conf = self.proxy.texts[-1]
        self.assertIn("server_name home.example.com;", conf)
        self.assertIn("server_name tunnel.example.com;", conf)
        self.assertIn("proxy_pass http://homeassistant:8123;", conf)
        self.assertIn("trusted_proxies:\n    - 172.30.33.0/24", self.ha_path.read_text())
        self.assertTrue(self.app._state("ha_restart").get("pending"))
        status = json.loads((self.ssl / "ssl-cert-manager" / "status.json").read_text())
        self.assertTrue(status["proxy"]["enabled"])
        self.app.panel_toggle("local_proxy", False)
        self.app._tick()
        self.assertGreater(self.proxy.stopped, 0)

    def test_proxy_options_reload_the_proxy(self) -> None:
        self.act("letsencrypt", {"text": "home.example.com"})
        self.app.panel_toggle("local_proxy", True)
        self.app._tick()
        self.drain()
        self.assertNotIn("Strict-Transport-Security", self.proxy.texts[-1])
        self.sup.stored = {**self.sup.stored, "proxy_hsts": "max-age=31536000", "proxy_real_ip": "cloudflare"}
        self.app.check_options()
        self.app._tick()
        self.assertIn('Strict-Transport-Security "max-age=31536000"', self.proxy.texts[-1])
        self.assertIn("real_ip_header CF-Connecting-IP;", self.proxy.texts[-1])
        self.assertIn("From Cloudflare", self.state_text())

    def test_home_assistant_https_on_and_off(self) -> None:
        self.act("letsencrypt", {"text": "home.example.com"})
        self.act("ha_https_on")
        text = self.ha_path.read_text()
        self.assertIn("ssl_certificate: ", text)
        self.assertTrue(self.app._state("ha_https")["enabled"])
        self.assertTrue(self.app._state("ha_restart")["pending"])
        self.act("restart_ha")
        self.assertEqual(self.sup.restarted, 1)
        # A renewed default certificate needs another restart.
        self.act("renew:home-example-com")
        self.assertTrue(self.app._state("ha_restart").get("pending"))
        self.act("ha_https_off")
        self.assertEqual(self.ha_path.read_text(), "default_config:\n")
        self.assertIn("Home Assistant itself on HTTPS", self.state_text())

    def test_home_assistant_https_needs_a_certificate(self) -> None:
        self.act("ha_https_on")
        assert self.app.last_job is not None
        self.assertEqual(self.app.last_job, ("Add a certificate first", "error"))
        self.assertNotIn("ssl_certificate", self.ha_path.read_text())

    # -- the tunnel check ---------------------------------------------------------------------
    def write_tunnel(self, hostname: str = "home.example.com") -> None:
        self.tunnel_file.parent.mkdir(parents=True, exist_ok=True)
        self.tunnel_file.write_text(
            json.dumps({"hostname": hostname, "verify": {"state": "ok", "message": "Verified"}})
        )

    def test_tunnel_check_after_a_change(self) -> None:
        self.write_tunnel()
        self.probes = [502, 502, 401]
        self.act("letsencrypt", {"text": "home.example.com"})
        check = self.app._state("tunnel_check")
        self.assertEqual(check["state"], "ok")
        self.assertEqual(self.sup.asked, "a1b2c3d4_cloudflare_tunnel")
        self.assertIn(main.TUNNEL_SETTLE_SECONDS, self.sleeps)

    def test_tunnel_check_failure_notifies(self) -> None:
        self.write_tunnel()
        self.drain()  # the first publish pass checks the tunnel too
        self.probes = [502, 502, 502]
        self.act("tunnel_check")
        check = self.app._state("tunnel_check")
        self.assertEqual(check["state"], "error")
        self.assertIn("origin TLS", str(check["message"]))
        self.assertEqual(self.sup.sent[-1][1], "Cloudflare Tunnel check failed")
        self.assertIn("Failing", self.state_text())

    def test_tunnel_not_installed(self) -> None:
        self.sup.tunnel_state = None
        self.act("tunnel_check")
        self.assertEqual(self.app._state("tunnel_check")["state"], "absent")


class HelpersTest(unittest.TestCase):
    def test_sibling_slug(self) -> None:
        self.assertEqual(
            main.sibling_slug("a1b2c3d4_ssl_cert_manager", "cloudflare_tunnel"), "a1b2c3d4_cloudflare_tunnel"
        )
        self.assertEqual(main.sibling_slug("local_ssl_cert_manager", "cloudflare_tunnel"), "local_cloudflare_tunnel")
        self.assertEqual(main.sibling_slug("", "cloudflare_tunnel"), "cloudflare_tunnel")

    def test_make_id(self) -> None:
        self.assertEqual(main.make_id("*.example.com", set()), "wildcard-example-com")
        self.assertEqual(main.make_id("home.example.com", {"home-example-com"}), "home-example-com-2")
        self.assertEqual(main.make_id("default", set()), "default-cert")

    def test_describe_reach(self) -> None:
        self.assertEqual(main.describe_reach(401)[1], "ok")
        self.assertEqual(main.describe_reach(526)[1], "error")
        self.assertEqual(main.describe_reach(None)[1], "warn")

    def test_config_validation(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.load_config({"renew_days_before": 3})
        with self.assertRaises(config.ConfigError):
            config.load_config({"acme_email": "nope"})
        with self.assertRaises(config.ConfigError):
            config.load_config({"proxy_hsts": "max-age=1; evil"})
        with self.assertRaises(config.ConfigError):
            config.load_config({"proxy_real_ip": "everyone"})
        self.assertEqual(
            config.load_config({"proxy_hsts": "max-age=31536000; includeSubDomains"}).proxy_real_ip, "apps"
        )
        self.assertEqual(config.load_config({}).renew_days_before, 30)
        self.assertEqual(ha_config.network_for("172.30.33.7"), "172.30.33.0/24")


if __name__ == "__main__":
    unittest.main()
