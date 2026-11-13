"""The shared ingress panel server: what it refuses and what every answer carries."""

from __future__ import annotations

import http.client
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import webui

APP = Path(webui.__file__).resolve().parent
GOOD = {"Content-Type": "application/json", webui.REQUEST_HEADER: "1"}


class _App:
    """A stand-in for an app: records what reaches it."""

    def __init__(self) -> None:
        self.toggles: list[tuple[str, bool]] = []
        self.actions: list[tuple[str, webui.PanelArgs]] = []

    def panel_state(self) -> dict[str, object]:
        return {"title": "Test", "subtitle": "", "sections": []}

    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        self.toggles.append((toggle_id, value))

    def panel_action(self, action_id: str, args: webui.PanelArgs) -> str | None:
        if action_id == "boom":
            raise RuntimeError("secret detail")
        self.actions.append((action_id, args))
        return "done"


class PanelServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _App()
        router = webui.PanelRouter(self.app)
        self.server = webui.PanelServer(APP / "web", router.get_state, router.on_toggle, router.on_action, port=0)
        self.server.start()
        self.addCleanup(self.server.stop)

    def request(
        self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()

    def post(self, path: str, payload: object, headers: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
        status, _headers, body = self.request(
            "POST", path, json.dumps(payload).encode(), GOOD if headers is None else headers
        )
        return status, json.loads(body)

    # -- what gets through ---------------------------------------------------------
    def test_a_toggle_from_the_page_reaches_the_app(self) -> None:
        status, reply = self.post("/api/toggle", {"id": "notify", "value": True})
        self.assertEqual((status, reply["ok"]), (200, True))
        self.assertEqual(self.app.toggles, [("notify", True)])

    def test_an_action_gets_its_arguments_as_panel_args(self) -> None:
        status, reply = self.post("/x/api/action", {"id": "rename", "args": {"text": "  kitchen  "}})
        self.assertEqual((status, reply["message"]), (200, "done"))
        action_id, args = self.app.actions[0]
        self.assertEqual(action_id, "rename")
        self.assertIsInstance(args, webui.PanelArgs)
        self.assertEqual(args.text(), "kitchen")

    def test_same_origin_fetch_metadata_is_accepted(self) -> None:
        status, _reply = self.post(
            "/api/toggle", {"id": "a", "value": False}, {**GOOD, "Sec-Fetch-Site": "same-origin"}
        )
        self.assertEqual(status, 200)

    # -- cross-site requests (CSRF) ------------------------------------------------
    def test_a_change_without_the_panel_header_is_refused(self) -> None:
        status, reply = self.post("/api/toggle", {"id": "a", "value": True}, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)
        self.assertIn("Reload", str(reply["error"]))
        self.assertEqual(self.app.toggles, [])

    def test_a_form_style_post_is_refused(self) -> None:
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            with self.subTest(content_type=content_type):
                headers = {"Content-Type": content_type, webui.REQUEST_HEADER: "1"}
                status, _reply = self.post("/api/action", {"id": "rename", "args": {}}, headers)
                self.assertEqual(status, 415)
        self.assertEqual(self.app.actions, [])

    def test_a_request_from_another_site_is_refused(self) -> None:
        for site in ("cross-site", "same-site"):
            with self.subTest(site=site):
                status, _reply = self.post("/api/toggle", {"id": "a", "value": True}, {**GOOD, "Sec-Fetch-Site": site})
                self.assertEqual(status, 403)
        self.assertEqual(self.app.toggles, [])

    # -- malformed requests ---------------------------------------------------------
    def test_content_length_is_checked_before_reading(self) -> None:
        cases = {
            "-5": 400,
            "abc": 400,
            str(webui.MAX_BODY_BYTES + 1): 413,
        }
        for length, expected in cases.items():
            with self.subTest(length=length):
                connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
                self.addCleanup(connection.close)
                connection.putrequest("POST", "/api/toggle")
                for name, value in {**GOOD, "Content-Length": length}.items():
                    connection.putheader(name, value)
                connection.endheaders()
                self.assertEqual(connection.getresponse().status, expected)

    def test_chunked_bodies_are_refused(self) -> None:
        status, _headers, _body = self.request("POST", "/api/toggle", b"", {**GOOD, "Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)

    def test_invalid_json_is_a_400_not_a_crash(self) -> None:
        for body in (b"{", b"[1]", b"[" * 100_000, b"\xff\xfe"):
            with self.subTest(body=body[:8]):
                status, _headers, reply = self.request("POST", "/api/toggle", body, GOOD)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(reply))

    def test_toggle_value_must_be_a_boolean(self) -> None:
        status, _reply = self.post("/api/toggle", {"id": "a", "value": "yes"})
        self.assertEqual(status, 400)

    def test_an_app_error_does_not_leak_its_details(self) -> None:
        with self.assertLogs("webui", level="ERROR"):
            status, reply = self.post("/api/action", {"id": "boom"})
        self.assertEqual(status, 500)
        self.assertNotIn("secret detail", json.dumps(reply))

    def test_unknown_paths_and_methods(self) -> None:
        self.assertEqual(self.post("/api/other", {})[0], 404)
        self.assertEqual(self.request("PUT", "/api/toggle", b"{}", GOOD)[0], 501)

    # -- answers --------------------------------------------------------------------
    def test_every_answer_carries_the_security_headers(self) -> None:
        answers = [
            self.request("GET", "/"),
            self.request("GET", "/panel.js"),
            self.request("GET", "/api/state"),
            self.request("GET", "/missing.js"),
            self.request("POST", "/api/toggle", b"{}", {}),
        ]
        for status, headers, _body in answers:
            with self.subTest(status=status):
                self.assertIn("frame-ancestors 'self'", headers["content-security-policy"])
                self.assertIn("default-src 'none'", headers["content-security-policy"])
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["x-frame-options"], "SAMEORIGIN")
                self.assertEqual(headers["referrer-policy"], "no-referrer")
                self.assertEqual(headers["cache-control"], "no-store")

    def test_static_files_have_fixed_content_types(self) -> None:
        expected = {
            "/": "text/html; charset=utf-8",
            "/panel.js": "text/javascript; charset=utf-8",
            "/icons.js": "text/javascript; charset=utf-8",
            "/panel.css": "text/css; charset=utf-8",
        }
        for path, content_type in expected.items():
            with self.subTest(path=path):
                status, headers, _body = self.request("GET", path)
                self.assertEqual((status, headers["content-type"]), (200, content_type))

    def test_only_files_in_the_web_folder_are_served(self) -> None:
        for path in ("/../webui.py", "/..%2Fwebui.py", "/webui.py", "/web/../../webui.py", "/.env"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_other_clients_are_dropped_without_an_answer(self) -> None:
        original = webui.ALLOWED_CLIENTS
        webui.ALLOWED_CLIENTS = frozenset({"172.30.32.2"})
        self.addCleanup(setattr, webui, "ALLOWED_CLIENTS", original)
        with self.assertRaises((http.client.HTTPException, OSError)):
            self.request("GET", "/api/state")


class PanelPageTest(unittest.TestCase):
    """The page must work under the Content-Security-Policy the server sends."""

    def test_the_page_sends_the_header_the_server_requires(self) -> None:
        script = (APP / "web" / "panel.js").read_text(encoding="utf-8")
        self.assertIn(f'"{webui.REQUEST_HEADER}": "1"', script)

    def test_no_inline_script_or_style(self) -> None:
        page = (APP / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", page), "inline <script> is blocked by the CSP")
        self.assertIsNone(
            re.search(r"<style|\sstyle=|\son[a-z]+=", page), "inline style/handlers are blocked by the CSP"
        )
        script = (APP / "web" / "panel.js").read_text(encoding="utf-8")
        for blocked in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(blocked, script)
        self.assertIsNone(re.search(r"\bstyle:\s", script), "style attributes are blocked by the CSP; use el.style")


class SecretRefsTest(unittest.TestCase):
    def test_resolve_options_keeps_the_shape(self) -> None:
        secrets = webui.SecretRefs()
        secrets.learn({"token": "!secret api_token", "n": 1}, {"token": "placeholder-value", "n": 1})
        stored: dict[str, object] = {"token": "!secret api_token", "rows": [{"t": "!secret api_token"}], "n": 1}
        self.assertEqual(
            secrets.resolve_options(stored),
            {"token": "placeholder-value", "rows": [{"t": "placeholder-value"}], "n": 1},
        )
        self.assertEqual(stored["token"], "!secret api_token")  # the stored copy keeps the reference
        with self.assertRaises(webui.SecretError):
            secrets.resolve_options({"token": "!secret unknown"})


class PanelArgsTest(unittest.TestCase):
    def test_text_is_stripped_and_bounded(self) -> None:
        args = webui.PanelArgs({"text": "  home.example.com \n", "n": 5, "long": "x" * 50})
        self.assertEqual(args.text(), "home.example.com")
        self.assertEqual(args.text("missing"), "")
        with self.assertRaises(webui.PanelError):
            args.text("n")
        with self.assertRaises(webui.PanelError):
            args.text("long", max_len=10)
        with self.assertRaises(webui.PanelError):
            args.text("missing", required=True)

    def test_text_refuses_control_characters(self) -> None:
        for value in ("a\nb", "a\x00b", "a\x1bb", "a\rb"):
            with self.subTest(value=value), self.assertRaises(webui.PanelError):
                webui.PanelArgs({"text": value}).text()

    def test_ident_must_match_the_pattern(self) -> None:
        pattern = re.compile(r"[a-z0-9_]+")
        self.assertEqual(webui.PanelArgs({"account": "home_1"}).ident("account", pattern), "home_1")
        for value in ("", "../etc", "home 1", None, 7):
            with self.subTest(value=value), self.assertRaises(webui.PanelError):
                webui.PanelArgs({"account": value}).ident("account", pattern)

    def test_flag_is_a_real_boolean(self) -> None:
        self.assertTrue(webui.PanelArgs({"value": True}).flag("value"))
        self.assertTrue(webui.PanelArgs({}).flag("value", default=True))
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(webui.PanelError):
                webui.PanelArgs({"value": value}).flag("value")

    def test_files_limits(self) -> None:
        self.assertEqual(webui.PanelArgs({"files": [{"name": "a.pem", "text": "x"}]}).files(), [("a.pem", "x")])
        too_many = [{"name": f"{i}.pem", "text": "x"} for i in range(webui.MAX_UPLOAD_FILES + 1)]
        too_big = [{"name": "a.pem", "text": "x" * (webui.MAX_UPLOAD_BYTES + 1)}]
        for rows in ([], too_many, too_big, [{"name": "a.pem", "text": 5}], "nope"):
            with self.subTest(rows=str(rows)[:30]), self.assertRaises(webui.PanelError):
                webui.PanelArgs({"files": rows}).files()


if __name__ == "__main__":
    unittest.main()
