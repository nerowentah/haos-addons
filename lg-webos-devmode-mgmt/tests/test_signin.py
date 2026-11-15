"""Tests for telling a dead session token (a new sign-in on the TV) from other renewal failures."""

from __future__ import annotations

import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import run

OK = '{"result":"success","errorCode":"200","errorMsg":"GNL"}'
LEFT = '{"result":"success","errorCode":"200","errorMsg":"999:30:00"}'
STALE = '{"result":"fail","errorCode":"ERR_005","errorMsg":"Check user session"}'
LOGIN_PAGE = '<html><form><input type="password" name="pw"></form></html>'
OPTIONS: dict[str, object] = {
    "interval_hours": 168,
    "retries": 2,
    "notify_on_failure": True,
    "maintenance_windows": [],
    "sessions": [{"name": "TV", "token": "placeholder-token"}],
}


class Reply:
    """A canned urlopen answer."""

    def __init__(self, body: str, url: str | None = None, status: int = 200) -> None:
        self.body, self.url, self.status = body, url, status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body.encode()

    def geturl(self) -> str:
        return self.url or ""


class SignInTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.opened: list[str] = []
        self.notified: list[str] = []

        def notify(_session: run.Session, _targets: list[str], _token: str, reason: str = "failed") -> None:
            self.notified.append(reason)

        self.patch(run, "notify_failure", notify)

    def patch(self, target: object, name: str, replacement: object) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, reset: Reply | Exception, check: Reply | Exception | None = None) -> None:
        """Answer the renewal address with ``reset`` and the status address with ``check``."""

        def urlopen(url: str, **_kwargs: object) -> Reply:
            self.opened.append(url.partition("?")[0])
            reply = check if url.startswith(run.LG_CHECK_ENDPOINT) else reset
            if reply is None:
                raise AssertionError("the status address was not expected to be asked")
            if isinstance(reply, Exception):
                raise reply
            if reply.url is None:
                reply.url = url  # no redirect
            return reply

        self.patch(urllib.request, "urlopen", urlopen)

    def session(self) -> run.Session:
        return run.load_config(OPTIONS).sessions[0]

    def app(self) -> run.App:
        app = run.App(run.load_config(OPTIONS), OPTIONS, "token", phones_path=Path(self.tmp.name) / "phones.json")
        self.patch(app, "check_options", lambda: None)
        self.patch(app, "refresh_phones", lambda: None)
        self.patch(app.stopping, "wait", lambda _seconds: None)  # no real back-off sleep
        return app

    def test_answers_to_a_renewal(self) -> None:
        cases = {
            OK: "ok",
            STALE: "signin",
            '{"result":"fail","errorCode":"ERR_001","errorMsg":"Something else"}': "failed",
            LOGIN_PAGE: "signin",
            "": "failed",  # used to count as a success
            "<html>hello</html>": "failed",
            "[1, 2]": "failed",
        }
        for body, outcome in cases.items():
            with self.subTest(body=body):
                self.assertEqual(run.handle_response(body, 200, "TV").outcome, outcome)
        self.assertEqual(run.handle_response("", 200, "TV", redirected=True).outcome, "signin")

    def test_redirect_to_another_page_is_a_sign_in(self) -> None:
        self.serve(Reply("<html>welcome</html>", url="https://developer.lge.com/account/start"))
        self.assertEqual(run.renew_session(self.session()).outcome, "signin")
        self.assertEqual(self.opened, [run.LG_DEV_ENDPOINT])  # nothing to verify after that

    def test_good_renewal_reports_the_time_left(self) -> None:
        self.serve(Reply(OK), Reply(LEFT))
        result = run.renew_session(self.session())
        self.assertEqual((result.outcome, result.remaining_hours), ("ok", 999.5))
        self.assertEqual(self.opened, [run.LG_DEV_ENDPOINT, run.LG_CHECK_ENDPOINT])

    def test_status_answers(self) -> None:
        cases: dict[str, tuple[Reply | Exception, str]] = {
            "no time in the text": (Reply(OK), "ok"),
            "not json": (Reply("<html>hello</html>"), "ok"),
            "unknown session": (Reply(STALE), "signin"),
            "sign-in page": (Reply(LOGIN_PAGE), "signin"),
            "unreachable": (urllib.error.URLError("down"), "ok"),
        }
        for name, (check, outcome) in cases.items():
            with self.subTest(name):
                self.serve(Reply(OK), check)
                result = run.renew_session(self.session())
                self.assertEqual((result.outcome, result.remaining_hours), (outcome, None))

    def test_which_sessions_can_be_verified(self) -> None:
        lg = "https://developer.lge.com/secure/ResetDevModeSession.dev?sessionToken=placeholder-token"
        own, other = run.build_sessions(
            [{"name": "a", "url": lg}, {"name": "b", "url": "https://example.invalid/renew?sessionToken=x"}]
        )
        self.assertEqual(
            own.check_url.reveal() if own.check_url else None, f"{run.LG_CHECK_ENDPOINT}?sessionToken=placeholder-token"
        )
        self.assertIsNone(other.check_url)
        self.serve(Reply(OK))  # asking the status address would fail the test
        self.assertTrue(run.renew_session(other).ok)

    def test_sign_in_is_not_retried_and_alerts_once(self) -> None:
        app = self.app()
        self.serve(Reply(STALE))
        app._tick()
        self.assertEqual(self.opened, [run.LG_DEV_ENDPOINT])  # retries: 2, yet one call
        status = app.status["tv"]
        self.assertTrue(status.needs_signin)
        self.assertFalse(status.last_ok)
        card = app.panel_state()["sections"][0]["cards"][0]  # type: ignore[index]
        self.assertEqual(card["badge"], {"text": "Sign in on TV", "tone": "error"})

        app.next_run = 0
        app._tick()
        self.assertEqual(self.notified, ["signin"])  # still stale: no second alert

        self.serve(Reply(OK), Reply(LEFT))
        app.next_run = 0
        app._tick()
        self.assertFalse(status.needs_signin)
        card = app.panel_state()["sections"][0]["cards"][0]  # type: ignore[index]
        self.assertEqual(card["badge"]["text"], "Renewed")
        self.assertIn({"label": "Time left", "value": "999", "unit": "h"}, card["stats"])

    def test_other_failures_are_retried_and_alert_as_before(self) -> None:
        app = self.app()
        self.serve(Reply(""))
        app._tick()
        self.assertEqual(len(self.opened), 3)  # first try + 2 retries
        self.assertEqual(self.notified, ["failed"])
        self.assertFalse(app.status["tv"].needs_signin)


if __name__ == "__main__":
    unittest.main()
