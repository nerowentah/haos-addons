"""Tests for session tokens: !secret references kept, https-only custom URLs (no network)."""

from __future__ import annotations

import copy
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import redact
import run
import webui

STORED: dict[str, object] = {
    "interval_hours": 48,
    "retries": 3,
    "notify_on_failure": True,
    "sessions": [{"name": "TV", "token": "!secret lg_tv_token"}],
}
RESOLVED: dict[str, object] = {**STORED, "sessions": [{"name": "TV", "token": "tok-123"}]}


class UrlTest(unittest.TestCase):
    def test_custom_url_must_be_https(self) -> None:
        for url in ("http://developer.lge.com/secure/ResetDevModeSession.dev?sessionToken=x", "https://", "ftp://x"):
            with self.subTest(url=url), self.assertRaises(run.ConfigError):
                run.build_sessions([{"name": "TV", "url": url}])
        ok = run.build_sessions([{"name": "TV", "url": "https://developer.lge.com/secure/x.dev?sessionToken=x"}])
        self.assertTrue(ok[0].custom_url)

    def test_url_option_is_masked(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("url: password?", text)


class SecretOptionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stored = copy.deepcopy(STORED)
        self.reachable = True
        self.written: list[dict[str, object]] = []

        def fetch(_token: str) -> dict[str, object]:
            if not self.reachable:
                raise OSError("unreachable")
            return copy.deepcopy(self.stored)

        def write(_token: str, options: dict[str, object]) -> None:
            self.written.append(copy.deepcopy(options))

        for name, replacement in (("fetch_options", fetch), ("write_options", write)):
            patcher = mock.patch.object(run, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        secrets = webui.SecretRefs()
        raw = webui.initial_options(lambda: copy.deepcopy(RESOLVED), lambda: fetch("t"), secrets)
        self.app = run.App(
            run.load_config(secrets.resolve_options(raw)),
            raw,
            "token",
            phones_path=Path(self.tmp.name) / "phones.json",
        )
        self.app.secrets = secrets

    def test_live_change_keeps_the_resolved_token(self) -> None:
        self.stored["interval_hours"] = 24
        self.app.check_options()
        self.assertEqual(self.app.config.interval_hours, 24)
        self.assertIn("tok-123", self.app.config.sessions[0].url.reveal())
        self.assertNotIn("secret", self.app.config.sessions[0].url.reveal())

    def test_session_token_never_shows_in_repr_or_logs(self) -> None:
        session = run.build_sessions([{"name": "TV", "token": "tok-123456789"}])[0]
        self.assertNotIn("tok-123456789", repr(session))
        self.assertNotIn("tok-123456789", str(session.url))
        record = logging.LogRecord("x", logging.INFO, "", 0, "opening %s", (session.url.reveal(),), None)
        line = redact.RedactingFormatter(logging.Formatter("%(message)s")).format(record)
        self.assertTrue(line.startswith("opening "))
        self.assertNotIn("tok-123456789", line)

    def test_panel_change_writes_the_reference(self) -> None:
        self.app.update_options(lambda raw: raw.update(notify_on_failure=False))
        self.assertEqual(self.written[-1]["sessions"], [{"name": "TV", "token": "!secret lg_tv_token"}])

    def test_panel_change_without_the_supervisor_writes_nothing(self) -> None:
        self.reachable = False
        with self.assertRaises(webui.PanelError):
            self.app.update_options(lambda raw: raw.update(notify_on_failure=False))
        self.assertEqual(self.written, [])


if __name__ == "__main__":
    unittest.main()
