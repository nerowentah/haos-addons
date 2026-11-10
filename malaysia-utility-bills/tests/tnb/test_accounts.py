"""Tests for one-login-many-accounts resolution, masking and setup mode."""

from __future__ import annotations

import io
import sys
import threading
import unittest
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "rootfs" / "app"))

from core import webui
from providers.tnb import config, mytnb

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

    def test_pasted_link_wins_and_keeps_encoded_cano(self) -> None:
        url = "https://myaccount.mytnb.com.my/AccountManagement/SmartMeter/Index/TRIL?caNo=AbC%2B12%3D%3D&x=1"
        cfg = config.load_config(
            {**LOGIN, "accounts": [account("Home", account_number="123456789012", smartmeter_url=url)]}
        )
        self.assertEqual(cfg.accounts[0].ca_no, "AbC%2B12%3D%3D")
        self.assertEqual(
            cfg.accounts[0].smartmeter_path, "/AccountManagement/SmartMeter/Index/TRIL?caNo=AbC%2B12%3D%3D"
        )


class _FakeSession(mytnb.MyTnbSession):
    """Replays canned portal responses instead of touching the network."""

    def __init__(self, ca_no: str, pages: dict[str, mytnb._Response]) -> None:
        super().__init__("you@example.com", "pw", ca_no)
        self.pages = pages
        self.requested: list[str] = []

    def _send_raw(self, url, *, data=None, headers=None):  # type: ignore[override]  # noqa: ARG002
        self.requested.append(url)
        for prefix, response in self.pages.items():
            if url.startswith(prefix):
                return response
        raise AssertionError(url)


def _ok(body: str, url: str = "") -> mytnb._Response:
    return mytnb._Response(status=200, body=body, content_type="text/html", url=url)


TRIL = "/AccountManagement/SmartMeter/Index/TRIL?caNo="


class PortalLoginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._sleep = mytnb.time.sleep
        mytnb.time.sleep = lambda _s: None

    def tearDown(self) -> None:
        mytnb.time.sleep = self._sleep

    def pages(self, account_page: str, dashboard: str) -> dict[str, mytnb._Response]:
        return {
            mytnb.LOGIN_URL: _ok('<input name="Token" value="t"/>'),
            mytnb.SSO_URL: _ok(account_page, "https://myaccount.mytnb.com.my/AccountManagement/Home"),
            mytnb.ACCOUNT_BASE + TRIL: _ok("meter page", "https://myaccount.mytnb.com.my/x"),
            mytnb.DASHBOARD_URL: _ok(dashboard, mytnb.DASHBOARD_URL),
        }

    def test_finds_links_and_matches_plain_number(self) -> None:
        body = f'<a href="{TRIL}123456789012">View</a> <a href="{TRIL}123456789013&amp;y=1">View</a>'
        links = mytnb.find_smartmeter_links(body)
        self.assertEqual(links, ["123456789012", "123456789013"])
        self.assertEqual(mytnb.pick_smartmeter_ca("123456789013", links), "123456789013")
        self.assertIsNone(mytnb.pick_smartmeter_ca("123456789014", links))
        self.assertIsNone(mytnb.pick_smartmeter_ca("123456789014", ["999"]))  # never guesses a single link

    def test_handoff_form_is_parsed_and_posted(self) -> None:
        page = (
            '<form id="formSmartMeterRequest" action="https://smartliving.myaccount.mytnb.com.my/login?renew" '
            'method="post"><input id="USERTOKEN" name="USERTOKEN" type="hidden" value="tok&amp;en" />'
            '<input id="CA_NO" name="CA_NO" type="hidden" value="AbC%3D" /></form>'
            '<input id="btnProceed" form="formSmartMeterRequest" type="submit" value="Go" />'
        )
        action, fields = mytnb.find_handoff_form(page)  # type: ignore[misc]
        self.assertEqual(action, "https://smartliving.myaccount.mytnb.com.my/login?renew")
        self.assertEqual(fields, {"USERTOKEN": "tok&en", "CA_NO": "AbC%3D"})

        pages = self.pages("", "<html>dashboard never reached</html>")
        pages["https://smartliving.myaccount.mytnb.com.my/login"] = _ok('{"sdpudcid":"777"}', mytnb.DASHBOARD_URL)
        pages[mytnb.ACCOUNT_BASE + TRIL] = _ok(page, "https://myaccount.mytnb.com.my/x")
        session = _FakeSession("123456789012", pages)
        self.assertEqual(session.login(), "777")
        self.assertIn("https://smartliving.myaccount.mytnb.com.my/login?renew", session.requested)

    def test_handoff_form_never_posts_off_site(self) -> None:
        for action in ("https://evil.example.com/login", "http://smartliving.myaccount.mytnb.com.my/login"):
            page = f'<form action="{action}"><input name="USERTOKEN" type="hidden" value="t"/></form>'
            self.assertIsNone(mytnb.find_handoff_form(page), action)

    def test_login_returns_meter_id(self) -> None:
        session = _FakeSession("123456789012", self.pages(f"{TRIL}123456789012", '{"sdpudcid":"4242"}'))
        self.assertEqual(session.login(), "4242")

    def test_missing_meter_is_not_an_auth_error(self) -> None:
        session = _FakeSession("123456789012", self.pages(f"{TRIL}SomeToken%3D", "<html>no id</html>"))
        with self.assertRaises(mytnb.MeterNotFoundError) as ctx:
            session.login()
        message = str(ctx.exception)
        self.assertNotIsInstance(ctx.exception, mytnb.AuthError)
        self.assertIn("1 smart meter link(s)", message)
        self.assertIn("none with this account number", message)
        self.assertIn("smartmeter_url", message)
        self.assertNotIn("123456789012", message)  # account numbers never reach the log
        self.assertNotIn("SomeToken", message)

    def test_debug_trace_has_no_secrets(self) -> None:
        page = (
            '<title>Hi 123456789012</title><input name="Token" value="s3cr3t"/>'
            f'<a href="{TRIL}123456789012">View</a> {{"sdpudcid":"4242"}}'
        )
        session = _FakeSession("123456789012", self.pages(page, '{"sdpudcid":"4242"}'))
        session._jar.set_cookie(
            mytnb.http.cookiejar.Cookie(
                0,
                "ASP.NET_SessionId",
                "cookievalue",
                None,
                False,
                "myaccount.mytnb.com.my",
                True,
                False,
                "/",
                True,
                True,
                None,
                False,
                None,
                None,
                {},
            )
        )
        session._jar.clear = lambda: None  # keep the planted cookie through login()
        mytnb.set_debug(True)
        try:
            with self.assertLogs(mytnb.__name__, "DEBUG") as logs:
                session.login()
        finally:
            mytnb.set_debug(False)
        trace = " | ".join(logs.output)
        for secret in ("pw", "you@example.com", "s3cr3t", "cookievalue", "123456789012", "4242", "caNo="):
            self.assertNotIn(secret, trace.replace("[login]", ""))
        self.assertIn("ASP.NET_SessionId", trace)
        self.assertIn("form fields [Token]", trace)
        self.assertIn("••••9012", trace)


class _FakeHTTPResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status = status
        self._headers = headers
        self._body = body
        raw = "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        self.msg = mytnb.http.client.parse_headers(io.BytesIO(raw.encode()))

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name, default)

    def info(self):
        return self.msg

    def read(self) -> bytes:
        return self._body


class _FakeConnection:
    """Scripted HTTPS connection: records each request, replays responses in order."""

    script: ClassVar[list[_FakeHTTPResponse]] = []
    sent: ClassVar[list[tuple[str, str, list[str]]]] = []
    opened = 0

    def __init__(self, host: str, timeout: float) -> None:  # noqa: ARG002
        self.host = host
        type(self).opened += 1
        self._headers: list[str] = []

    def putrequest(self, method: str, target: str, **_kw: object) -> None:
        self._method, self._target, self._headers = method, target, []

    def putheader(self, name: str, value: str) -> None:
        self._headers.append(f"{name}: {value}")

    def endheaders(self, body: bytes | None = None) -> None:  # noqa: ARG002
        type(self).sent.append((self._method, f"{self.host}{self._target}", self._headers))

    def getresponse(self) -> _FakeHTTPResponse:
        return type(self).script.pop(0)

    def close(self) -> None:
        pass


class KeepAliveClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self._conn = mytnb.http.client.HTTPSConnection
        mytnb.http.client.HTTPSConnection = _FakeConnection  # type: ignore[misc]
        _FakeConnection.sent, _FakeConnection.opened = [], 0

    def tearDown(self) -> None:
        mytnb.http.client.HTTPSConnection = self._conn  # type: ignore[misc]

    def test_redirect_chain_reuses_connection_keeps_cookies_and_casing(self) -> None:
        host = "smartliving.myaccount.mytnb.com.my"
        _FakeConnection.script = [
            _FakeHTTPResponse(302, {"Location": "/login", "Set-Cookie": "e3AuthToken=abc; Path=/; Secure"}),
            _FakeHTTPResponse(302, {"Location": f"https://{host}/dashboard"}),
            _FakeHTTPResponse(200, {"Content-Type": "text/html"}, b'{"sdpudcid":"1"}'),
        ]
        session = mytnb.MyTnbSession("you@example.com", "pw", "123456789012")
        response = session._send_raw(f"https://{host}/login?renew", data={"USERTOKEN": "t"}, headers=mytnb.HTML_HEADERS)
        self.assertEqual((response.status, response.url), (200, f"https://{host}/dashboard"))
        self.assertEqual(_FakeConnection.opened, 1)  # one kept-alive connection for the whole chain
        methods = [m for m, _u, _h in _FakeConnection.sent]
        self.assertEqual(methods, ["POST", "GET", "GET"])  # 302 turns the POST into a GET
        first, last = _FakeConnection.sent[0][2], _FakeConnection.sent[-1][2]
        self.assertIn("User-Agent", [h.split(":")[0] for h in first])  # browser casing, not User-agent
        self.assertIn("DNT: 1", first)
        self.assertIn("Connection: keep-alive", first)
        self.assertIn("Cookie: e3AuthToken=abc", last)
        self.assertFalse(any(h.startswith("Content-Type") for h in last))

    def test_endless_redirects_are_a_loop(self) -> None:
        _FakeConnection.script = [
            _FakeHTTPResponse(302, {"Location": "/login"}) for _ in range(mytnb.MAX_REDIRECTS + 1)
        ]
        session = mytnb.MyTnbSession("you@example.com", "pw", "123456789012")
        with self.assertRaises(mytnb.AuthError):
            session._send_raw("https://smartliving.myaccount.mytnb.com.my/login")


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
