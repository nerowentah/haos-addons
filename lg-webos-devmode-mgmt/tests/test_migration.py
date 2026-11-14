"""Tests for moving the retired notification_overrides option into /data."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import run

OPTIONS: dict[str, object] = {
    "interval_hours": 168,
    "retries": 3,
    "notify_on_failure": True,
    "sessions": [{"name": "TV", "token": "tok"}],
}


class MigrateOverridesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.phones = Path(self.tmp.name) / "phones.json"
        self.written: list[dict[str, object]] = []

        def write(_token: str, options: dict[str, object]) -> None:
            self.written.append(options)

        self.patch("write_options", write)

    def patch(self, name: str, replacement: object) -> None:
        patcher = mock.patch.object(run, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, stored: dict[str, object], phones: dict[str, bool] | None = None) -> run.App:
        self.patch("fetch_options", lambda _token: stored)
        if phones is not None:
            self.phones.write_text(json.dumps(phones), encoding="utf-8")
        # /data/options.json never has the key: the Supervisor filters it to the schema.
        return run.App(run.load_config(OPTIONS), dict(OPTIONS), "token", phones_path=self.phones)

    def test_removes_key_found_only_in_stored_options(self) -> None:
        stored = {
            **OPTIONS,
            "notification_overrides": [
                {"name": "mobile_app_phone_a", "notify": False},
                {"name": "mobile_app_phone_b", "notify": True},
            ],
        }
        app = self.app(stored, phones={"mobile_app_phone_a": True})
        app.migrate_overrides()
        self.assertEqual(len(self.written), 1)
        self.assertNotIn("notification_overrides", self.written[0])
        self.assertEqual(self.written[0]["sessions"], OPTIONS["sessions"])
        self.assertNotIn("notification_overrides", app.raw_options)
        # Switches already in /data win; old ones are only added when missing.
        self.assertEqual(app.phones, {"mobile_app_phone_a": True, "mobile_app_phone_b": True})
        self.assertEqual(json.loads(self.phones.read_text(encoding="utf-8")), app.phones)

    def test_nothing_to_do_without_the_key(self) -> None:
        app = self.app(dict(OPTIONS))
        app.migrate_overrides()
        self.assertEqual(self.written, [])

    def test_falls_back_to_local_options_when_supervisor_is_unreachable(self) -> None:
        app = self.app(dict(OPTIONS))

        def boom(_token: str) -> dict[str, object]:
            raise OSError("down")

        self.patch("fetch_options", boom)
        app.migrate_overrides()
        self.assertEqual(self.written, [])


if __name__ == "__main__":
    unittest.main()
