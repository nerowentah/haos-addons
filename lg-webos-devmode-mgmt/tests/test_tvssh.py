"""Tests for reading the session token from the TV over SSH and acting on what it holds."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Self, cast
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import redact
import run
import tvssh
import webui

SAVED = "a" * 64
NEW = "b" * 64
HOST = "192.0.2.10"
TV: dict[str, object] = {"host": HOST, "passphrase": "ABC123", "media_player": "media_player.tv"}
KEY = b"-----BEGIN RSA PRIVATE KEY-----\nplaceholder\n-----END RSA PRIVATE KEY-----\n"
BODY = "QUJD" * 40  # 160 base64 characters: three lines of a key file
OPENSSH_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    + "\n".join(BODY[i : i + 70] for i in range(0, len(BODY), 70))
    + "\n-----END OPENSSH PRIVATE KEY-----\n"
)
LEGACY_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF\n\n"
    + "\n".join(BODY[i : i + 64] for i in range(0, len(BODY), 64))
    + "\n-----END RSA PRIVATE KEY-----\n"
)
PUBLIC_KEY = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC placeholder"


def options(**session: object) -> dict[str, object]:
    return {
        "interval_hours": 168,
        "retries": 0,
        "notify_on_failure": True,
        "maintenance_windows": [],
        "sessions": [{"name": "TV", "token": SAVED, **TV, **session}],
    }


class Reply:
    """A canned urlopen answer."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self.body


class ConfigTest(unittest.TestCase):
    def test_tv_options(self) -> None:
        session = run.load_config(options(port=2222)).sessions[0]
        self.assertEqual((session.port, session.media_player), (2222, "media_player.tv"))
        self.assertEqual(session.host, redact.Secret(HOST))
        self.assertEqual(run.load_config(options()).sessions[0].port, 9922)
        self.assertNotIn(HOST, repr(session))
        self.assertNotIn("ABC123", repr(session))

    def test_bad_tv_options_are_refused(self) -> None:
        bad: list[dict[str, object]] = [
            {"passphrase": ""},  # required with host
            {"host": "tv.example.invalid"},  # an IP address only
            {"host": "-oProxyCommand=x"},
            {"port": 0},
            {"port": 70000},
            {"port": True},
            {"media_player": "light.tv"},
            {"host": "", "passphrase": ""},  # media_player alone
        ]
        for change in bad:
            with self.subTest(change), self.assertRaises(run.ConfigError):
                run.load_config(options(**change))

    def test_sessions_without_host_are_unchanged(self) -> None:
        raw = options()
        raw["sessions"] = [{"name": "TV", "token": SAVED}]
        session = run.load_config(raw).sessions[0]
        self.assertIsNone(session.host)
        self.assertIsNone(session.passphrase)


class SshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.tv = tvssh.Tv(redact.Secret(HOST), 2222, redact.Secret("ABC123"), folder / "key", folder / "known")

    def patch(self, target: object, name: str, replacement: object) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_command_has_no_passphrase_and_no_shell(self) -> None:
        command = tvssh.ssh_command(self.tv)
        self.assertEqual(command[:3], ["ssh", "-p", "2222"])
        self.assertEqual(command[-3:], [f"prisoner@{HOST}", "cat", tvssh.TOKEN_FILE])
        self.assertNotIn("ABC123", " ".join(command))
        self.assertIn("-oStrictHostKeyChecking=accept-new", command)

    def test_results(self) -> None:
        self.assertEqual(tvssh.parse_result(0, f"  {SAVED}\r\n", ""), SAVED)
        cases: list[tuple[int, str, str, type[tvssh.TvError]]] = [
            (0, "", "", tvssh.NoTokenError),
            (1, "", "cat: can't open '/var/luna/preferences/devmode_enabled'", tvssh.NoTokenError),
            (0, "not a token!", "", tvssh.TvError),
            (255, "", "prisoner@host: Permission denied (publickey,keyboard-interactive).", tvssh.KeyRejectedError),
            (255, "", "Load key: incorrect passphrase supplied to decrypt private key", tvssh.KeyRejectedError),
            (255, "", "@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @", tvssh.HostKeyChangedError),
            (255, "", "Host key verification failed.", tvssh.HostKeyChangedError),
            (255, "", "ssh: connect to host port 2222: Connection timed out", tvssh.UnreachableError),
            (255, "", "ssh: connect to host port 2222: Connection refused", tvssh.UnreachableError),
        ]
        for code, out, err, error in cases:
            with self.subTest(err or out), self.assertRaises(error) as caught:
                tvssh.parse_result(code, out, err)
            self.assertIs(type(caught.exception), error)

    def test_read_passes_the_passphrase_by_environment(self) -> None:
        seen: dict[str, object] = {}

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            seen.update(kwargs, command=command)
            return subprocess.CompletedProcess(command, 0, SAVED + "\n", "")

        self.patch(shutil, "which", lambda _name: "/usr/bin/ssh")
        self.patch(subprocess, "run", fake_run)
        self.assertEqual(tvssh.read_token(self.tv), SAVED)
        env = cast("dict[str, str]", seen["env"])
        self.assertEqual(env[tvssh.PASSPHRASE_ENV], "ABC123")
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertNotIn("shell", seen)
        self.assertNotIn("ABC123", " ".join(cast("list[str]", seen["command"])))

    def test_timeout_is_unreachable(self) -> None:
        def slow(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(command, 1)

        self.patch(shutil, "which", lambda _name: "/usr/bin/ssh")
        self.patch(subprocess, "run", slow)
        with self.assertRaises(tvssh.UnreachableError):
            tvssh.read_token(self.tv)

    def test_session_time_parsing(self) -> None:
        self.assertEqual(tvssh.parse_session_text("980h:12m"), 980 + 12 / 60)
        self.assertEqual(tvssh.parse_session_text(" 0h:00m "), 0.0)
        for text in ("--:--", "999:30:00", "", "1000", "soon"):
            with self.subTest(text):
                self.assertIsNone(tvssh.parse_session_text(text))
        expires_in_100_hours = str(int((time.time() + 100 * 3600) * 1000))  # an expiry as epoch milliseconds
        self.assertAlmostEqual(cast("float", tvssh.parse_session_text(expires_in_100_hours)), 100.0, delta=0.01)
        self.assertEqual(tvssh.parse_session_text(str(int((time.time() - 3600) * 1000))), 0.0)  # already expired

    def test_session_answer_parsing(self) -> None:
        self.assertEqual(tvssh.parse_remain_session('{"remainSession":"980h:12m"}'), 980 + 12 / 60)
        for text in ('{"returnValue":false}', "[]", "not json", '{"remainSession":"--:--"}'):
            with self.subTest(text):
                self.assertIsNone(tvssh.parse_remain_session(text))

    def test_session_read_asks_the_tv_service_first(self) -> None:
        calls: list[tuple[str, ...]] = []

        def answer(_tv: tvssh.Tv, remote: Sequence[str]) -> subprocess.CompletedProcess[str]:
            calls.append(tuple(remote))
            return subprocess.CompletedProcess(list(remote), 0, '{"returnValue":true,"remainSession":"980h:12m"}', "")

        self.patch(tvssh, "_ssh", answer)
        self.assertAlmostEqual(cast("float", tvssh.read_session(self.tv)), 980 + 12 / 60)
        self.assertEqual(calls, [("luna-send-pub", "-n", "1", "-f", tvssh.SESSION_STATUS_URI, "{}")])
        self.assertNotIn("cat", " ".join(calls[0]))

    def test_session_read_falls_back_to_the_file(self) -> None:
        answers = [
            subprocess.CompletedProcess([], 127, "", "luna-send-pub: not found"),
            subprocess.CompletedProcess([], 0, "975h:05m\n", ""),
        ]

        def answer(_tv: tvssh.Tv, remote: Sequence[str]) -> subprocess.CompletedProcess[str]:
            self.assertIn(remote[0], {"luna-send-pub", "cat"})
            return answers.pop(0)

        self.patch(tvssh, "_ssh", answer)
        self.assertAlmostEqual(cast("float", tvssh.read_session(self.tv)), 975 + 5 / 60)

    def test_session_read_is_none_when_nothing_answers(self) -> None:
        refused = [subprocess.CompletedProcess([], 1, "", "can't open"), subprocess.CompletedProcess([], 1, "", "no")]
        self.patch(tvssh, "_ssh", lambda _tv, _remote: refused.pop(0))
        self.assertIsNone(tvssh.read_session(self.tv))

        def unreachable(_tv: tvssh.Tv, _remote: Sequence[str]) -> subprocess.CompletedProcess[str]:
            raise tvssh.UnreachableError("off")

        self.patch(tvssh, "_ssh", unreachable)
        self.assertIsNone(tvssh.read_session(self.tv))  # never raises: the value is only cosmetic

    def test_key_download(self) -> None:
        opened: list[str] = []

        def urlopen(url: str, **_kwargs: object) -> Reply:
            opened.append(url)
            return Reply(KEY)

        self.patch(urllib.request, "urlopen", urlopen)
        tvssh.fetch_key(self.tv)
        self.assertEqual(opened, [f"http://{HOST}:9991/webos_rsa"])
        self.assertEqual(self.tv.key_path.read_bytes(), KEY)  # stored as received: still encrypted
        if os.name == "posix":
            self.assertEqual(self.tv.key_path.stat().st_mode & 0o777, 0o600)

        for body in (b"<html>hello</html>", b"-----BEGIN" + b"x" * tvssh.MAX_KEY_BYTES):
            self.patch(urllib.request, "urlopen", lambda _url, body=body, **_kw: Reply(body))
            with self.assertRaises(tvssh.KeyServerError):
                tvssh.fetch_key(self.tv)

        def down(_url: str, **_kwargs: object) -> Reply:
            raise urllib.error.URLError("refused")

        self.patch(urllib.request, "urlopen", down)
        with self.assertRaises(tvssh.KeyServerError):
            tvssh.fetch_key(self.tv)

    def test_refused_key_is_downloaded_again_once(self) -> None:
        self.tv.key_path.write_bytes(b"-----BEGIN old")
        fetched: list[int] = []
        answers: list[str | Exception] = [tvssh.KeyRejectedError("old key"), SAVED]

        def read(_tv: tvssh.Tv) -> str:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        self.patch(tvssh, "fetch_key", lambda _tv: fetched.append(1))
        self.patch(tvssh, "read_token", read)
        self.assertEqual(tvssh.read_token_with_key(self.tv), SAVED)
        self.assertEqual(fetched, [1])

        answers[:] = [tvssh.KeyRejectedError("old"), tvssh.KeyRejectedError("fresh: wrong passphrase")]
        with self.assertRaises(tvssh.KeyRejectedError):
            tvssh.read_token_with_key(self.tv)
        self.assertEqual(fetched, [1, 1])  # not a third download


class KeyTextTest(unittest.TestCase):
    def test_a_one_line_paste_is_repaired(self) -> None:
        for key in (OPENSSH_KEY, LEGACY_KEY):
            clean = tvssh.normalize_key(key)
            body = b"".join(line for line in clean.splitlines() if line and b"-" not in line and b":" not in line)
            self.assertEqual(body.decode(), BODY)
            self.assertTrue(clean.endswith(b"-----\n"))
            pastes = (key.replace("\n", " "), key.replace("\n", "\\n"), key.replace("\n", "\r\n"), f"  {key}  ")
            for paste in pastes:
                with self.subTest(paste[:40]):
                    self.assertEqual(tvssh.normalize_key(paste), clean)
        legacy = tvssh.normalize_key(LEGACY_KEY.replace("\n", " ")).decode()
        self.assertIn("\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF\n\n", legacy)

    def test_what_is_not_a_private_key_is_refused(self) -> None:
        cases = {
            PUBLIC_KEY: "public key",
            "-----BEGIN PUBLIC KEY-----\nQUJD\n-----END PUBLIC KEY-----": "public key",
            "---- BEGIN SSH2 PUBLIC KEY ----\nQUJD\n---- END SSH2 PUBLIC KEY ----": "no private key",
            "hello": "no private key",
            "-----BEGIN RSA PRIVATE KEY-----\n!!!\n-----END RSA PRIVATE KEY-----": "damaged",
            "-----BEGIN RSA PRIVATE KEY-----\n-----END RSA PRIVATE KEY-----": "damaged",
            "-----BEGIN RSA PRIVATE KEY-----\n" + "QUJD" * 5000 + "\n-----END RSA PRIVATE KEY-----": "too large",
        }
        for text, message in cases.items():
            with self.subTest(message), self.assertRaises(tvssh.KeyFormatError) as caught:
                tvssh.normalize_key(text)
            self.assertIn(message, str(caught.exception))

    def test_option(self) -> None:
        session = run.load_config(options(private_key=OPENSSH_KEY.replace("\n", " "))).sessions[0]
        self.assertEqual(session.private_key, redact.Secret(tvssh.normalize_key(OPENSSH_KEY).decode()))
        self.assertNotIn(BODY[:20], repr(session))
        for bad in ({"private_key": PUBLIC_KEY}, {"private_key": OPENSSH_KEY, "host": "", "media_player": ""}):
            with self.subTest(bad), self.assertRaises(run.ConfigError):
                run.load_config(options(**bad))


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.notified: list[str] = []
        self.checked: list[str] = []  # tokens LG was asked about
        self.renewed: list[str] = []
        self.stored: dict[str, object] = {}
        self.written: list[dict[str, object]] = []
        self.tv_answer: str | Exception = SAVED
        self.tv_hours: float | None = None  # what the TV's own countdown says
        self.own_key: list[bool] = []
        self.lg_accepts = True
        self.entity_state = "off"

        def notify(_session: run.Session, _targets: list[str], _token: str, reason: str = "failed") -> None:
            self.notified.append(reason)

        def check(session: run.Session) -> run.RenewResult:
            self.checked.append(session.token.reveal() if session.token else "")
            return run.RenewResult("ok", 999.5) if self.lg_accepts else run.RenewResult("signin")

        def renew(session: run.Session) -> run.RenewResult:
            self.renewed.append(session.token.reveal() if session.token else "")
            return run.RenewResult("ok")

        def read(_tv: tvssh.Tv, *, own_key: bool = False) -> str:
            self.own_key.append(own_key)
            if isinstance(self.tv_answer, Exception):
                raise self.tv_answer
            return self.tv_answer

        def supervisor(_token: str, path: str, _body: object | None = None) -> object:
            if path.startswith("/core/api/states/"):
                return {"entity_id": path.rsplit("/", 1)[-1], "state": self.entity_state}
            raise AssertionError(path)

        def write(_token: str, new: dict[str, object]) -> None:
            self.written.append(new)
            self.stored = new

        self.patch(run, "notify_failure", notify)
        self.patch(run, "check_session", check)
        self.patch(run, "renew_session", renew)
        self.real_read_with_key = tvssh.read_token_with_key
        self.patch(tvssh, "read_token_with_key", read)
        self.patch(tvssh, "read_session", lambda _tv: self.tv_hours)
        self.patch(run, "_supervisor_request", supervisor)
        self.patch(run, "fetch_options", lambda _token: self.stored)
        self.patch(run, "write_options", write)

    def patch(self, target: object, name: str, replacement: object) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, raw: dict[str, object] | None = None, resolved: dict[str, object] | None = None) -> run.App:
        self.stored = raw or options()
        app = run.App(
            run.load_config(resolved or self.stored), self.stored, "token", self.data / "phones.json", self.data
        )
        if resolved is not None:
            app.secrets.learn(self.stored, resolved)
        self.patch(app, "check_options", lambda: None)
        self.patch(app, "refresh_phones", lambda: None)
        app.next_run = time.time() + 3600  # no scheduled renewal unless a test asks for one
        return app

    def card(self, app: run.App) -> dict[str, object]:
        sections = cast("list[dict[str, list[dict[str, object]]]]", app.panel_state()["sections"])
        return sections[0]["cards"][0]

    def labels(self, app: run.App) -> list[object]:
        return [button["label"] for button in cast("list[dict[str, object]]", self.card(app)["actions"])]

    def due_now(self, app: run.App) -> None:
        for key, (_due, attempts) in list(app.tv_due.items()):
            app.tv_due[key] = (0.0, attempts)

    def test_turning_on_queues_one_read(self) -> None:
        app = self.app()
        app._tick()
        self.assertEqual(app.tv_due, {})  # the TV is off
        self.entity_state = "on"
        app._tick()
        self.assertEqual(list(app.tv_due), ["tv"])
        self.assertEqual(self.checked, [])  # not before the TV had time to start its SSH service
        self.due_now(app)
        app._tick()
        self.assertEqual(self.checked, [SAVED])
        app._tick()  # still on: nothing new
        self.assertEqual((app.tv_due, self.checked), ({}, [SAVED]))
        self.entity_state = "unavailable"
        app._tick()
        self.entity_state = "idle"
        app._tick()
        self.assertEqual(list(app.tv_due), ["tv"])

    def test_same_token_only_asks_lg(self) -> None:
        app = self.app()
        self.assertTrue(app.verify_tv(app.config.sessions[0], renew_after=True))
        status = app.status["tv"]
        self.assertEqual((status.tv_state, status.remaining_hours), ("Same as saved", 999.5))
        self.assertEqual((self.checked, self.notified, self.written, app.queue), ([SAVED], [], [], []))
        self.assertIn(
            {"label": "TV token", "value": "Same as saved", "unit": ""}, cast("list[object]", self.card(app)["stats"])
        )

    def test_the_tvs_own_countdown_is_shown_first(self) -> None:
        app = self.app()
        self.tv_hours = 980 + 12 / 60  # what the TV said: "980h:12m"
        app.verify_tv(app.config.sessions[0], renew_after=True)
        status = app.status["tv"]
        self.assertEqual((status.tv_remaining_hours, status.remaining_hours), (980 + 12 / 60, 999.5))
        stats = cast("list[dict[str, object]]", self.card(app)["stats"])
        self.assertIn({"label": "Time left", "value": "980", "unit": "h"}, stats)
        self.assertIn({"label": "LG reports", "value": "999", "unit": "h"}, stats)
        self.assertIn("Time left from", [entry["label"] for entry in stats])

    def test_without_a_tv_value_the_lg_one_stays(self) -> None:
        app = self.app()
        app.verify_tv(app.config.sessions[0], renew_after=True)
        stats = cast("list[dict[str, object]]", self.card(app)["stats"])
        self.assertIn({"label": "Time left", "value": "999", "unit": "h"}, stats)
        self.assertNotIn("LG reports", [entry["label"] for entry in stats])

    def test_a_large_gap_between_the_clocks_is_named_once(self) -> None:
        app = self.app()
        self.tv_hours = 900.0  # the TV's own counter lags 99 h behind LG's
        with self.assertLogs(run.__name__, level="WARNING") as logs:
            app.verify_tv(app.config.sessions[0], renew_after=True)
            app.verify_tv(app.config.sessions[0], renew_after=True)
        drift = [line for line in logs.output if "differs from LG" in line]
        self.assertEqual(len(drift), 1)
        self.assertIn("99 h", drift[0])
        self.tv_hours = 999.5  # the TV caught up: armed again
        app.verify_tv(app.config.sessions[0], renew_after=True)
        self.assertFalse(app.status["tv"].tv_drift_alerted)

    def test_changed_token_is_saved_announced_and_used(self) -> None:
        app = self.app()
        self.tv_answer = NEW
        app.verify_tv(app.config.sessions[0], renew_after=True)
        self.assertEqual((self.checked, self.notified), ([NEW], ["changed"]))
        self.assertEqual(cast("list[dict[str, object]]", self.written[0]["sessions"])[0]["token"], NEW)
        self.assertEqual(app.status["tv"].tv_state, "Updated from TV")
        app._tick()  # applies the saved options and runs the queued renewal
        self.assertEqual(self.renewed, [NEW])
        self.assertEqual(app.config.sessions[0].token, redact.Secret(NEW))
        app.verify_tv(app.config.sessions[0], renew_after=True)
        self.assertEqual(self.notified, ["changed"])  # no second alert
        self.assertEqual(app.status["tv"].tv_state, "Same as saved")

    def test_secret_reference_is_never_overwritten(self) -> None:
        reference = "!secret tv_token"
        stored = options(token=reference)
        app = self.app(stored, options())
        self.tv_answer = NEW
        app.verify_tv(app.config.sessions[0], renew_after=True)
        self.assertEqual((self.written, self.notified), ([], ["changed_secret"]))
        self.assertEqual(app.status["tv"].tv_state, "Changed on TV (not saved)")
        app._tick()
        self.assertEqual(self.renewed, [NEW])  # renewed with the TV's token, from memory
        app.verify_tv(app.config.sessions[0], renew_after=True)
        self.assertEqual(self.notified, ["changed_secret"])  # known now: no second alert
        for path in self.data.rglob("*"):
            if path.is_file():
                self.assertNotIn(NEW.encode(), path.read_bytes())

    def test_token_lg_rejects_asks_for_a_sign_in(self) -> None:
        app = self.app()
        self.tv_answer, self.lg_accepts = NEW, False
        app.verify_tv(app.config.sessions[0], renew_after=True)
        app.verify_tv(app.config.sessions[0], renew_after=True)
        self.assertEqual((self.notified, self.written), (["signin"], []))
        self.assertEqual(self.card(app)["badge"], {"text": "Sign in on TV", "tone": "error"})

    def test_problems_on_the_tv_alert_once(self) -> None:
        cases: list[tuple[Exception, str, str]] = [
            (tvssh.NoTokenError("x"), "devmode_off", "Dev Mode off on TV"),
            (tvssh.KeyServerError("x"), "keyserver", "Turn on Key Server"),
            (tvssh.KeyRejectedError("x"), "passphrase", "Check the passphrase"),
            (tvssh.HostKeyChangedError("x"), "hostkey", "TV host key changed"),
        ]
        for error, reason, state in cases:
            with self.subTest(reason):
                self.notified.clear()
                app = self.app()
                app.status["tv"] = run.SessionStatus(last_ok=True)
                self.tv_answer = error
                self.assertTrue(app.verify_tv(app.config.sessions[0], renew_after=True))
                app.verify_tv(app.config.sessions[0], renew_after=True)
                self.assertEqual(self.notified, [reason])
                self.assertEqual(self.card(app)["badge"], {"text": state, "tone": "error"})
                self.tv_answer = SAVED
                app.verify_tv(app.config.sessions[0], renew_after=True)
                self.assertEqual(self.card(app)["badge"], {"text": "Renewed", "tone": "ok"})

    def test_unreachable_tv_is_retried_without_alert(self) -> None:
        app = self.app()
        self.tv_answer = tvssh.UnreachableError("off")
        app.tv_due["tv"] = (0.0, 2)
        app._tick()
        self.assertEqual(app.tv_due["tv"][1], 1)
        self.due_now(app)
        app._tick()
        self.assertEqual((app.tv_due, self.notified), ({}, []))
        self.assertEqual(app.status["tv"].tv_state, "TV not reachable")

    def test_scheduled_renewal_reads_the_tv_first(self) -> None:
        app = self.app()
        self.tv_answer = NEW
        app.next_run = 0
        app._tick()
        self.assertEqual(self.renewed, [NEW])  # one renewal, with the TV's token

    def test_no_call_to_lg_inside_a_maintenance_window(self) -> None:
        app = self.app({**options(), "maintenance_windows": ["daily 00:00-24:00"]})
        self.tv_answer = NEW
        app.verify_tv(app.config.sessions[0], renew_after=True)
        self.assertEqual((self.checked, app.queue), ([], []))
        self.assertEqual(app.status["tv"].tv_state, "Updated from TV")

    def test_key_from_the_options_never_asks_the_key_server(self) -> None:
        app = self.app(options(private_key=OPENSSH_KEY))
        session = app.config.sessions[0]
        app.verify_tv(session, renew_after=False)
        self.assertEqual(self.own_key, [True])
        stored = self.data / "keys" / "tv"
        self.assertEqual(stored.read_bytes(), tvssh.normalize_key(OPENSSH_KEY))
        if os.name == "posix":
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        card = self.card(app)
        self.assertIn({"label": "SSH key", "value": "From options", "unit": ""}, cast("list[object]", card["stats"]))
        upload = cast("list[dict[str, object]]", card["actions"])[-1]
        self.assertEqual((upload["label"], upload["disabled"]), ("Upload SSH key", True))
        with self.assertRaises(webui.PanelError):
            app.panel_action("upload_key", webui.PanelArgs(session="tv", files=[{"name": "k", "text": OPENSSH_KEY}]))

        self.tv_answer = tvssh.KeyRejectedError("old key")
        app.verify_tv(session, renew_after=False)
        app.verify_tv(session, renew_after=False)
        self.assertEqual(self.notified, ["key"])
        self.assertEqual(self.card(app)["badge"], {"text": "Replace the SSH key", "tone": "error"})

    def test_own_key_skips_the_key_server_in_tvssh(self) -> None:
        tv = tvssh.Tv(redact.Secret(HOST), 9922, redact.Secret("ABC123"), self.data / "key", self.data / "known")

        def refuse(_tv: tvssh.Tv) -> str:
            raise tvssh.KeyRejectedError("refused")

        def no_download(_tv: tvssh.Tv) -> None:
            raise AssertionError("the key server must not be asked")

        self.patch(tvssh, "read_token", refuse)
        self.patch(tvssh, "fetch_key", no_download)
        with (
            mock.patch.object(tvssh, "read_token_with_key", self.real_read_with_key),
            self.assertRaises(tvssh.KeyRejectedError),
        ):
            tvssh.read_token_with_key(tv, own_key=True)

    def test_upload(self) -> None:
        app = self.app()
        self.assertIn(
            {"label": "SSH key", "value": "None yet", "unit": ""}, cast("list[object]", self.card(app)["stats"])
        )
        app.status["tv"] = run.SessionStatus(tv_alert="keyserver", tv_state="Turn on Key Server")
        one = {"name": "tv_webos", "text": LEGACY_KEY.replace("\n", " ")}
        answer = app.panel_action("upload_key", webui.PanelArgs(session="tv", files=[one]))
        self.assertEqual(answer, "Key saved. Reading the token from TV...")
        self.assertEqual((self.data / "keys" / "tv").read_bytes(), tvssh.normalize_key(LEGACY_KEY))
        self.assertIsNone(app.status["tv"].tv_alert)
        self.assertIn("tv", app.tv_due)
        stats = cast("list[dict[str, str]]", self.card(app)["stats"])
        self.assertTrue(next(s["value"] for s in stats if s["label"] == "SSH key").startswith("Uploaded "))
        app._tick()
        self.assertEqual(self.own_key, [False])  # an uploaded key may still fall back to the key server

        for files in ([], [one, one], [{"name": "id.pub", "text": PUBLIC_KEY}]):
            with self.subTest(len(files)), self.assertRaises(webui.PanelError):
                app.panel_action("upload_key", webui.PanelArgs(session="tv", files=files))

    def test_panel_buttons(self) -> None:
        app = self.app()
        self.assertEqual(app.panel_action("check_tv", webui.PanelArgs(session="tv")), "Reading the token from TV...")
        self.assertIn("tv", app.tv_due)
        self.assertEqual(self.labels(app), ["Renew now", "Check TV now", "Upload SSH key"])
        pinned = app._known_hosts(app.config.sessions[0])
        pinned.parent.mkdir(parents=True)
        pinned.write_text("old identity")
        app.status["tv"] = run.SessionStatus(tv_alert="hostkey", tv_state="TV host key changed")
        self.assertIn("Trust this TV again", self.labels(app))
        app.panel_action("trust_tv", webui.PanelArgs(session="tv"))
        self.assertFalse(pinned.exists())
        with self.assertRaises(webui.PanelError):
            app.panel_action("check_tv", webui.PanelArgs(session="gone"))


if __name__ == "__main__":
    unittest.main()
