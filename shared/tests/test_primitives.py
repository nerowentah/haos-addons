"""safefile, jsonval, redact, safehttp and the Supervisor client's path handling."""

from __future__ import annotations

import io
import logging
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "stubs"))  # config.py, which supervisor.py imports

import jsonval
import redact
import safefile
import safehttp
import supervisor
from store import Store


class AtomicWriteTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_writes_and_reports_changes(self) -> None:
        path = self.folder / "nested" / "state.json"
        self.assertTrue(safefile.atomic_write(path, "one"))
        self.assertEqual(path.read_text(encoding="utf-8"), "one")
        self.assertFalse(safefile.atomic_write(path, "one"))
        self.assertTrue(safefile.atomic_write(path, b"two"))
        self.assertEqual(path.read_bytes(), b"two")

    def test_leaves_no_temporary_file_behind(self) -> None:
        path = self.folder / "state.json"
        safefile.atomic_write(path, "x")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["state.json"])

    def test_a_failed_write_keeps_the_old_file(self) -> None:
        path = self.folder / "state.json"
        safefile.atomic_write(path, "old")
        with mock.patch("safefile.os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            safefile.atomic_write(path, "new")
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["state.json"])

    @unittest.skipUnless(os.name == "posix", "file modes are a POSIX feature")
    def test_private_by_default_and_mode_is_enforced(self) -> None:
        path = self.folder / "token"
        safefile.atomic_write(path, "secret")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        safefile.atomic_write(path, "secret", safefile.PUBLIC)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        path.chmod(0o666)
        self.assertTrue(safefile.atomic_write(path, "secret", safefile.PRIVATE))  # same data, wrong mode
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_the_store_saves_through_it(self) -> None:
        store = Store(self.folder / "state.json")
        store.section("phones")["mobile_app_phone"] = True
        store.dirty = True
        store.save()
        self.assertFalse(store.dirty)
        again = Store(self.folder / "state.json")
        again.load()
        self.assertEqual(again.data, {"phones": {"mobile_app_phone": True}})


class JsonValTest(unittest.TestCase):
    def test_accessors_return_typed_values(self) -> None:
        doc = jsonval.as_dict({"name": "Home", "on": True, "n": 3, "rate": 1.5, "rows": ["a"], "sub": {"k": 1}})
        self.assertEqual(jsonval.get_str(doc, "name"), "Home")
        self.assertTrue(jsonval.get_bool(doc, "on"))
        self.assertEqual(jsonval.get_int(doc, "n"), 3)
        self.assertEqual(jsonval.get_float(doc, "rate"), 1.5)
        self.assertEqual(jsonval.get_float(doc, "n"), 3.0)
        self.assertEqual(jsonval.get_list(doc, "rows"), ["a"])
        self.assertEqual(jsonval.get_dict(doc, "sub"), {"k": 1})
        self.assertEqual(jsonval.str_list(doc["rows"]), ["a"])

    def test_missing_keys_use_the_default(self) -> None:
        self.assertEqual(jsonval.get_str({}, "name", "x"), "x")
        self.assertEqual(jsonval.get_int({"n": None}, "n", 7), 7)
        self.assertEqual(jsonval.get_dict({}, "sub"), {})
        self.assertEqual(jsonval.get_list({}, "rows"), [])

    def test_wrong_types_raise(self) -> None:
        wrong = [
            (jsonval.as_dict, ([],)),
            (jsonval.as_list, ({},)),
            (jsonval.get_str, ({"k": 5}, "k")),
            (jsonval.get_bool, ({"k": "true"}, "k")),
            (jsonval.get_int, ({"k": True}, "k")),
            (jsonval.get_int, ({"k": 1.5}, "k")),
            (jsonval.get_float, ({"k": "1"}, "k")),
            (jsonval.get_dict, ({"k": []}, "k")),
            (jsonval.get_list, ({"k": "x"}, "k")),
            (jsonval.str_list, ([1],)),
        ]
        for function, args in wrong:
            with self.subTest(function=function.__name__), self.assertRaises(jsonval.ShapeError):
                function(*args)
        self.assertTrue(issubclass(jsonval.ShapeError, ValueError))


class RedactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(setattr, redact, "_values", redact._values)

    def test_a_secret_never_prints_its_value(self) -> None:
        secret = redact.Secret("placeholder-token-value")
        self.assertEqual(str(secret), "***")
        self.assertEqual(repr(secret), "Secret(***)")
        self.assertEqual(f"{secret}", "***")
        self.assertNotIn("placeholder", repr({"token": secret}))
        self.assertEqual(secret.reveal(), "placeholder-token-value")
        self.assertTrue(secret)
        self.assertFalse(redact.Secret(""))
        self.assertEqual(secret, redact.Secret("placeholder-token-value"))
        self.assertNotEqual(secret, redact.Secret("another-placeholder"))

    def test_registered_values_are_masked_in_log_lines_and_tracebacks(self) -> None:
        redact.register("placeholder-supervisor-token")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("redact-test")
        logger.addHandler(handler)
        logger.propagate = False
        self.addCleanup(logger.removeHandler, handler)
        redact.install(logger)
        redact.install(logger)  # installing twice wraps once

        logger.warning("header was Bearer %s", "placeholder-supervisor-token")
        try:
            raise RuntimeError("failed with placeholder-supervisor-token")  # noqa: TRY301 - the traceback is the test
        except RuntimeError:
            logger.exception("call failed")
        output = stream.getvalue()
        self.assertNotIn("placeholder-supervisor-token", output)
        self.assertIn("WARNING header was Bearer ***", output)
        self.assertIn("RuntimeError: failed with ***", output)

    def test_short_values_are_not_registered(self) -> None:
        redact.register("on")
        self.assertEqual(redact.scrub("turned on"), "turned on")


class SafeHttpTest(unittest.TestCase):
    def test_only_https_is_allowed(self) -> None:
        for url in ("http://example.com/", "file:///etc/passwd", "ftp://example.com/", "//example.com/", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safehttp.fetch(url)

    def test_redirects_to_plain_http_are_refused(self) -> None:
        handler = safehttp._HttpsRedirects()
        request = urllib.request.Request("https://example.com/a")
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(request, io.BytesIO(), 302, "Found", Message(), "http://example.com/b")
        followed = handler.redirect_request(request, io.BytesIO(), 302, "Found", Message(), "https://example.com/b")
        assert followed is not None
        self.assertEqual(followed.full_url, "https://example.com/b")

    def test_the_answer_size_is_capped(self) -> None:
        self.assertEqual(safehttp.read_limited(io.BytesIO(b"x" * 10), 10), b"x" * 10)
        with self.assertRaises(safehttp.TooLargeError):
            safehttp.read_limited(io.BytesIO(b"x" * 11), 10)

    def test_certificates_are_verified(self) -> None:
        import ssl  # noqa: PLC0415 - only this test needs it

        context = safehttp.tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)


class _Recorder(supervisor.Supervisor):
    def __init__(self) -> None:
        super().__init__("placeholder-token")
        self.urls: list[str] = []

    def _request(self, url: str, method: str = "GET", body: object | None = None) -> object:  # noqa: ARG002
        self.urls.append(url)
        return {"data": {"state": "started"}}


class SupervisorPathTest(unittest.TestCase):
    def test_names_cannot_change_the_api_path(self) -> None:
        sup = _Recorder()
        sup.get_state("sensor.a/../../config?x=1")
        sup.addon_state("core_ssh/restart#")
        sup.notify(["mobile_app_phone/../x"], "t", "m", "id")
        # /core/api/states/<id>, /addons/<slug>/info, /core/api/services/notify/<name>
        for url, slashes in zip(sup.urls, (4, 3, 5), strict=True):
            path = url.removeprefix(supervisor.SUPERVISOR_API)
            with self.subTest(path=path):
                self.assertEqual(path.count("/"), slashes)
                self.assertNotIn("?", path)
                self.assertNotIn("#", path)
        self.assertEqual(sup.urls[0], f"{supervisor.SUPERVISOR_CORE_API}/states/sensor.a%2F..%2F..%2Fconfig%3Fx%3D1")
        self.assertEqual(sup.urls[1], f"{supervisor.SUPERVISOR_API}/addons/core_ssh%2Frestart%23/info")

    def test_ordinary_names_are_unchanged(self) -> None:
        sup = _Recorder()
        sup.get_state("sensor.energy_total")
        sup.notify(["mobile_app_phone"], "t", "m", "id")
        self.assertEqual(sup.urls[0], f"{supervisor.SUPERVISOR_CORE_API}/states/sensor.energy_total")
        self.assertEqual(sup.urls[1], f"{supervisor.SUPERVISOR_CORE_API}/services/notify/mobile_app_phone")

    def test_empty_and_dot_names_are_refused(self) -> None:
        for name in ("", ".", ".."):
            with self.subTest(name=name), self.assertRaises(supervisor.SupervisorError):
                supervisor._segment(name)

    def test_the_token_is_registered_for_redaction(self) -> None:
        self.addCleanup(setattr, redact, "_values", redact._values)
        with (
            mock.patch.dict(os.environ, {"SUPERVISOR_TOKEN": "placeholder-supervisor-token-2"}),
            mock.patch.object(supervisor, "merge_s6_environment"),
        ):
            self.assertEqual(supervisor.load_supervisor_token(), "placeholder-supervisor-token-2")
        self.assertEqual(redact.scrub("Bearer placeholder-supervisor-token-2"), "Bearer ***")

    def test_notification_ids_take_the_app_prefix(self) -> None:
        self.assertEqual(supervisor.notification_id("power_tnb_", "home-1"), "power_tnb_home_1")


if __name__ == "__main__":
    unittest.main()
