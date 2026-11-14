"""Tests for signing in and creating the tunnel with cloudflared (fake processes, no network)."""

from __future__ import annotations

import base64
import json
import logging
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import account as acc
import cloudflared as cf
from test_cloudflared import FakeProcess, FakeSpawn

TUNNEL_ID = "6ff42ae2-765d-4adf-8112-31c55c1551ef"
API_TOKEN = "cf-api-token-value"  # noqa: S105 - a fake token for the tests
LOGIN_URL = "https://dash.cloudflare.com/argotunnel?aud=&callback=https%3A%2F%2Flogin.cloudflareaccess.org%2Fabc"


def cert_pem() -> str:
    payload = json.dumps({"zoneID": "zone123", "accountID": "acct456", "apiToken": API_TOKEN}).encode()
    body = base64.b64encode(payload).decode()
    return f"-----BEGIN ARGO TUNNEL TOKEN-----\n{body}\n-----END ARGO TUNNEL TOKEN-----\n"


def wait_until(predicate: Callable[[], object], seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class AccountTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "cloudflared"
        self.http_calls: list[tuple[str, dict[str, str]]] = []
        self.http_reply: bytes | Exception = json.dumps({"result": {"name": "Example.com"}}).encode()

    def http_get(self, url: str, headers: dict[str, str]) -> bytes:
        self.http_calls.append((url, headers))
        if isinstance(self.http_reply, Exception):
            raise self.http_reply
        return self.http_reply

    def account(self, *processes: FakeProcess) -> tuple[acc.Account, FakeSpawn]:
        spawn = FakeSpawn(*processes)

        def run(argv: list[str], env: dict[str, str]) -> FakeProcess:
            process = spawn(argv, env)
            if "create" in argv and b"Created tunnel" in process.stdout.getvalue():
                # cloudflared writes the credentials file named by --credentials-file
                Path(argv[argv.index("--credentials-file") + 1]).write_text(json.dumps({"TunnelID": TUNNEL_ID}))
            return process

        return acc.Account(self.base, spawn=run, http_get=self.http_get), spawn

    def sign_in(self, account: acc.Account) -> None:
        account.prepare()
        account.cert.write_text(cert_pem(), encoding="utf-8")

    # -- sign-in ----------------------------------------------------------------------
    def test_login_shows_the_link_then_finishes_when_the_cert_arrives(self) -> None:
        output = (
            f"Please open the following URL and log in with your Cloudflare account:\n\n{LOGIN_URL}\n\n"
            "Leave cloudflared running to download the cert automatically.\n"
        ).encode()
        process = FakeProcess(output)
        account, spawn = self.account(process)
        account.prepare()
        account.cert.write_text(cert_pem(), encoding="utf-8")  # cloudflared saves it before exiting
        account.login.start()
        self.assertTrue(wait_until(lambda: account.login.state == acc.LOGIN_DONE))
        self.assertEqual(account.login.url, LOGIN_URL)
        self.assertEqual(spawn.argvs[0], [cf.CLOUDFLARED, "tunnel", "login"])
        self.assertEqual(spawn.envs[0]["HOME"], str(self.base))
        self.assertNotIn(cf.TOKEN_ENV, spawn.envs[0])

    def test_a_login_that_ends_without_a_cert_fails_with_the_reason(self) -> None:
        process = FakeProcess(b"2026-11-13T00:00:00Z ERR Failed to write the certificate: timeout\n", returncode=1)
        process.returncode = 1
        account, _spawn = self.account(process)
        account.login.start()
        self.assertTrue(wait_until(lambda: account.login.state == acc.LOGIN_FAILED))
        self.assertIn("Failed to write the certificate", account.login.error or "")
        self.assertFalse(account.signed_in())

    def test_a_second_start_while_waiting_is_ignored(self) -> None:
        account, spawn = self.account()
        account.login.state = acc.LOGIN_WAITING
        account.login.start()
        self.assertEqual(spawn.argvs, [])

    # -- zone name --------------------------------------------------------------------
    def test_zone_name_comes_from_the_api_with_the_cert_token_and_is_cached(self) -> None:
        account, _spawn = self.account()
        self.assertIsNone(account.zone_name())  # not signed in
        self.sign_in(account)
        self.assertEqual(account.zone_name(), "example.com")
        self.assertEqual(account.zone_name(), "example.com")
        self.assertEqual(len(self.http_calls), 1)
        url, headers = self.http_calls[0]
        self.assertTrue(url.endswith("/zones/zone123"))
        self.assertEqual(headers["Authorization"], f"Bearer {API_TOKEN}")
        self.assertEqual(account.zone, "example.com")

    def test_zone_lookup_failure_is_quiet_and_not_retried(self) -> None:
        account, _spawn = self.account()
        self.sign_in(account)
        self.http_reply = OSError("offline")
        self.assertIsNone(account.zone_name())
        self.assertIsNone(account.zone_name())
        self.assertEqual(len(self.http_calls), 1)

    # -- create / route / ingress -------------------------------------------------------
    def test_create_parses_the_id_and_retries_a_taken_name(self) -> None:
        taken = FakeProcess(
            b"failed to create tunnel: Create Tunnel API call failed: tunnel with name already exists\n"
        )
        taken.returncode = 1
        created = FakeProcess(
            f"Tunnel credentials written to {self.base}/tunnel.json.\nCreated tunnel home-assistant-2 with id {TUNNEL_ID}\n".encode()
        )
        created.returncode = 0
        account, spawn = self.account(taken, created)
        self.sign_in(account)
        account.credentials.write_text(json.dumps({"TunnelID": TUNNEL_ID}), encoding="utf-8")
        self.assertEqual(account.create_tunnel(), (TUNNEL_ID, "home-assistant-2"))
        self.assertEqual(spawn.argvs[0][-1], "home-assistant")
        self.assertEqual(spawn.argvs[1][-1], "home-assistant-2")
        self.assertEqual(spawn.argvs[1][:4], [cf.CLOUDFLARED, "tunnel", "--origincert", str(account.cert)])
        self.assertIn("--credentials-file", spawn.argvs[1])

    def test_create_reports_other_errors(self) -> None:
        failed = FakeProcess(b"2026-11-13T00:00:00Z ERR Unauthorized: invalid origin certificate\n")
        failed.returncode = 1
        account, _spawn = self.account(failed)
        self.sign_in(account)
        with self.assertRaises(acc.AccountError) as ctx:
            account.create_tunnel()
        self.assertIn("invalid origin certificate", str(ctx.exception))

    def test_create_needs_a_sign_in(self) -> None:
        account, spawn = self.account()
        with self.assertRaises(acc.AccountError):
            account.create_tunnel()
        self.assertEqual(spawn.argvs, [])

    def test_route_dns_ok_exists_and_overwrite(self) -> None:
        ok = FakeProcess(b"INF Added CNAME home.example.com which will route to this tunnel\n")
        ok.returncode = 0
        exists = FakeProcess(
            b"Failed to add route: code: 1003, reason: An A, AAAA, or CNAME record with that host already exists.\n"
        )
        exists.returncode = 1
        account, spawn = self.account(ok, exists, FakeProcess(b""))
        self.assertEqual(account.route_dns(TUNNEL_ID, "home.example.com"), acc.ROUTE_OK)
        self.assertEqual(account.route_dns(TUNNEL_ID, "home.example.com"), acc.ROUTE_EXISTS)
        spawn.pending[0].returncode = 0
        account.route_dns(TUNNEL_ID, "home.example.com", overwrite=True)
        self.assertEqual(spawn.argvs[0][-4:], ["route", "dns", TUNNEL_ID, "home.example.com"])
        self.assertIn("--overwrite-dns", spawn.argvs[2])

    def test_route_dns_other_errors_raise(self) -> None:
        failed = FakeProcess(b"ERR Failed to add route: code: 1000, reason: Zone not found\n")
        failed.returncode = 1
        account, _spawn = self.account(failed)
        with self.assertRaises(acc.AccountError) as ctx:
            account.route_dns(TUNNEL_ID, "home.example.com")
        self.assertIn("Zone not found", str(ctx.exception))

    def test_ingress_sends_the_hostname_to_home_assistant_and_everything_else_to_404(self) -> None:
        account, _spawn = self.account()
        changed = account.write_ingress(
            TUNNEL_ID, "home.example.com", "https://homeassistant:8123", origin_request={"noTLSVerify": True}
        )
        self.assertTrue(changed)
        text = account.ingress.read_text(encoding="utf-8")
        self.assertIn(f"tunnel: {TUNNEL_ID}\n", text)
        self.assertIn(f"credentials-file: {account.credentials}\n", text)
        self.assertIn("  - hostname: home.example.com\n    service: https://homeassistant:8123\n", text)
        self.assertIn("    originRequest:\n      noTLSVerify: true\n", text)
        self.assertTrue(text.rstrip().endswith("- service: http_status:404"))
        self.assertFalse(account.has_previous())  # nothing was there before

    def test_ingress_with_ca_pool_keeps_the_previous_file_until_verified(self) -> None:
        account, _spawn = self.account()
        account.write_ingress(TUNNEL_ID, "home.example.com", "http://homeassistant:8123")
        first = account.ingress.read_text(encoding="utf-8")
        self.assertNotIn("originRequest", first)
        self.assertFalse(account.write_ingress(TUNNEL_ID, "home.example.com", "http://homeassistant:8123"))
        account.write_ingress(
            TUNNEL_ID,
            "home.example.com",
            "https://a1b2c3d4-ssl-cert-manager:8443",
            origin_request={
                "caPool": "/ssl/ssl-cert-manager/trust/ca-bundle.pem",
                "originServerName": "home.example.com",
            },
        )
        text = account.ingress.read_text(encoding="utf-8")
        self.assertIn("      caPool: /ssl/ssl-cert-manager/trust/ca-bundle.pem\n", text)
        self.assertIn("      originServerName: home.example.com\n", text)
        self.assertTrue(account.has_previous())
        self.assertTrue(account.restore_previous())
        self.assertEqual(account.ingress.read_text(encoding="utf-8"), first)
        self.assertFalse(account.has_previous())
        self.assertFalse(account.restore_previous())

    def test_tunnel_id_comes_from_the_credentials(self) -> None:
        account, _spawn = self.account()
        self.assertIsNone(account.tunnel_id())
        account.prepare()
        account.credentials.write_text(json.dumps({"TunnelID": TUNNEL_ID, "TunnelSecret": "x"}), encoding="utf-8")
        self.assertEqual(account.tunnel_id(), TUNNEL_ID)

    # -- sign out -----------------------------------------------------------------------
    def test_sign_out_deletes_the_tunnel_and_the_files(self) -> None:
        deleted = FakeProcess(b"")
        deleted.returncode = 0
        account, spawn = self.account(deleted)
        self.sign_in(account)
        account.credentials.write_text(json.dumps({"TunnelID": TUNNEL_ID}), encoding="utf-8")
        account.write_ingress(TUNNEL_ID, "home.example.com", "http://homeassistant:8123")
        self.assertIsNone(account.sign_out())
        self.assertEqual(spawn.argvs[0][-3:], ["delete", "-f", TUNNEL_ID])
        self.assertFalse(account.signed_in())
        self.assertIsNone(account.tunnel_id())
        self.assertFalse(account.ingress.exists())

    def test_sign_out_warns_when_cloudflare_keeps_the_tunnel(self) -> None:
        failed = FakeProcess(b"ERR Tunnel has active connections\n")
        failed.returncode = 1
        account, _spawn = self.account(failed)
        self.sign_in(account)
        account.credentials.write_text(json.dumps({"TunnelID": TUNNEL_ID}), encoding="utf-8")
        warning = account.sign_out()
        self.assertIn("Zero Trust", warning or "")
        self.assertFalse(account.signed_in())

    # -- secrets ------------------------------------------------------------------------
    def test_the_api_token_never_reaches_the_log(self) -> None:
        created = FakeProcess(f"Created tunnel home-assistant with id {TUNNEL_ID}\n".encode())
        created.returncode = 0
        ok = FakeProcess(b"")
        ok.returncode = 0
        account, _spawn = self.account(created, ok)
        self.sign_in(account)
        with self.assertLogs(level=logging.DEBUG) as logs:
            account.zone_name()
            account.create_tunnel()
            account.route_dns(TUNNEL_ID, "home.example.com")
            account.write_ingress(TUNNEL_ID, "home.example.com", "http://homeassistant:8123")
        self.assertNotIn(API_TOKEN, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
