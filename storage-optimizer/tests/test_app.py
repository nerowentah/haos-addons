"""Tests for the app: the panel works off a cached scan, and nothing is deleted unless it was asked for."""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config
import database
import main
import store
import webui
from helpers import GB, STORED, FakeSupervisor, usage_reply

NOW = dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.UTC)


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "homeassistant"
        self.root.mkdir()
        (self.root / "home-assistant.log").write_bytes(b"a" * 400)
        (self.root / "home-assistant.log.1").write_bytes(b"b" * 600)
        (self.root / database.DB_NAME).write_bytes(b"d" * 5000)
        (self.root / ".storage").mkdir()
        (self.root / ".storage" / "auth").write_bytes(b"s" * 50)
        self.sup = FakeSupervisor()
        self.store = store.Store(self.dir / "state.json")
        self.app = self._app(STORED)

    def _app(self, stored: dict[str, object]) -> main.App:
        self.sup.stored = copy.deepcopy(stored)
        secrets = webui.SecretRefs()
        raw = webui.initial_options(lambda: copy.deepcopy(stored), self.sup.fetch_options, secrets)
        app = main.App(
            config.load_config(secrets.resolve_options(raw)),
            self.sup,
            self.store,
            raw,
            ha_root=self.root,
            clock=lambda: NOW,
        )
        app.secrets = secrets
        return app

    def _drain(self) -> None:
        """Run the queued jobs here, as the worker thread would."""

        while not self.app.jobs.empty():
            label, job, rescan = self.app.jobs.get()
            self.app.run_job(label, job, rescan=rescan)

    def _state_text(self) -> str:
        return json.dumps(self.app.panel_state())

    def _action(self, action_id: str) -> str | None:
        return self.app.panel_action(action_id, webui.PanelArgs())

    def _cards(self) -> dict[str, dict[str, object]]:
        cards: dict[str, dict[str, object]] = {}
        sections = self.app.panel_state()["sections"]
        assert isinstance(sections, list)
        for section in sections:
            for card in section["cards"]:
                cards[str(card["title"])] = card
        return cards

    # -- the panel ---------------------------------------------------------------------------
    def test_before_the_first_scan_the_panel_still_answers(self) -> None:
        text = self._state_text()
        self.assertIn("Scanning...", text)
        self.assertEqual(self.sup.calls, [])

    def test_the_panel_is_built_from_the_cached_scan(self) -> None:
        self.app.run_scan()
        before = list(self.sup.calls)
        cards = self._cards()
        self.assertEqual(self.sup.calls, before)
        for title in (
            "Overview",
            "By area",
            "App data, per app",
            "Home Assistant, per folder",
            "Recorder database",
            "Backups",
            "Logs and caches",
            "Not cleaned here",
            "Settings",
            "Phones",
        ):
            self.assertIn(title, cards)
        text = self._state_text()
        self.assertIn("12.0 GB", text)  # free
        self.assertIn("Example App", text)  # the app's name instead of its folder
        self.assertIn("Host journal", text)
        self.assertNotIn("placeholder-token", text)
        areas = cards["By area"]["links"]
        assert isinstance(areas, list)
        self.assertEqual(len(areas), 8)

    def test_the_scan_asks_for_the_tree_with_a_long_timeout(self) -> None:
        self.app.run_scan()
        method, path, _body, timeout = self.sup.calls[0]
        self.assertEqual((method, path), ("GET", "/host/disks/default/usage?max_depth=2"))
        self.assertEqual(timeout, main.SCAN_TIMEOUT)

    def test_a_failed_usage_report_does_not_hide_the_rest(self) -> None:
        self.sup.usage = None
        self.app.run_scan()
        cards = self._cards()
        self.assertIn("did not report disk usage", json.dumps(cards["Overview"]))
        self.assertIn("toggles", cards["Backups"])
        self.assertIn("Recorder database", cards)

    def test_no_log_card_when_there_is_no_log_file(self) -> None:
        (self.root / "home-assistant.log").unlink()
        (self.root / "home-assistant.log.1").unlink()
        self.app.run_scan()
        self.assertNotIn("Logs and caches", self._cards())

    def test_only_local_unprotected_backups_have_a_switch(self) -> None:
        self.app.run_scan()
        toggles = self._cards()["Backups"]["toggles"]
        assert isinstance(toggles, list)
        self.assertEqual(
            sorted(t["id"] for t in toggles),
            [f"backup:{slug}" for slug in ("aaaa0001", "aaaa0002", "aaaa0003", "aaaa0004", "dddd0001")],
        )
        self.assertIn("2 backups not listed", json.dumps(self._cards()["Backups"]))

    def test_the_history_gets_one_sample_per_day(self) -> None:
        self.app.run_scan()
        self.sup.usage = usage_reply(used=22 * GB)
        self.app.run_scan()
        history = self.store.section("history")
        self.assertEqual(list(history), ["2026-01-10"])
        self.assertEqual(history["2026-01-10"], {"free": 10 * GB, "total": 32 * GB})
        self.assertIn("Free space, last 30 days", self._state_text())

    # -- nothing happens unless asked ---------------------------------------------------------
    def test_a_default_install_cleans_nothing(self) -> None:
        self.app.run_scan()
        self.app.next_auto_check = 0.0
        self.app._tick()
        self._drain()
        self.assertEqual(self.sup.deleted(), [])
        self.assertEqual(self.sup.purges(), [])
        self.assertEqual((self.root / "home-assistant.log").stat().st_size, 400)
        self.assertTrue((self.root / "home-assistant.log.1").exists())
        self.assertEqual(self.app.auto_clean(), "Automatic clean-up is off")

    def test_automatic_clean_up_leaves_backups_alone_without_the_second_switch(self) -> None:
        self.app = self._app({**STORED, "auto_clean": True})
        self.app._tick()
        self._drain()
        self.assertEqual(self.sup.deleted(), [])
        self.assertEqual(self.sup.purges(), [{"repack": False}])
        self.assertEqual((self.root / "home-assistant.log").stat().st_size, 0)
        self.assertFalse((self.root / "home-assistant.log.1").exists())
        self.assertEqual((self.root / database.DB_NAME).stat().st_size, 5000)
        self.assertEqual((self.root / ".storage" / "auth").stat().st_size, 50)

    def test_the_backup_switch_alone_does_nothing(self) -> None:
        self.app = self._app({**STORED, "auto_clean_backups": True})
        self.app._tick()
        self._drain()
        self.assertEqual(self.sup.deleted(), [])
        self.assertEqual(self.sup.purges(), [])

    def test_with_both_switches_old_backups_go_and_the_newest_stay(self) -> None:
        self.app = self._app({**STORED, "auto_clean": True, "auto_clean_backups": True, "keep_backups": 2})
        self.app._tick()
        self._drain()
        self.assertEqual(
            self.sup.deleted(),
            [("/backups/aaaa0002", None), ("/backups/aaaa0001", None), ("/backups/dddd0001", {"location": ".local"})],
        )
        left = {str(row["slug"]) for row in self.sup.backups}
        self.assertEqual(left, {"aaaa0003", "aaaa0004", "bbbb0001", "cccc0001"})

    def test_automatic_clean_up_runs_once_a_day(self) -> None:
        self.app = self._app({**STORED, "auto_clean": True})
        self.app._tick()
        self._drain()
        self.app.next_auto_check = 0.0
        self.app._tick()
        self._drain()
        self.assertEqual(len(self.sup.purges()), 1)

    # -- buttons ---------------------------------------------------------------------------------
    def test_deleting_needs_a_selection_and_only_deletes_it(self) -> None:
        self.app.run_scan()
        with self.assertRaises(webui.PanelError):
            self._action("delete_backups")
        self.app.panel_toggle("backup:aaaa0001", True)
        self.app.panel_toggle("backup:aaaa0003", True)
        self.app.panel_toggle("backup:aaaa0003", False)
        self._action("delete_backups")
        self._drain()
        self.assertEqual(self.sup.deleted(), [("/backups/aaaa0001", None)])
        self.assertEqual(self.app.selected, set())
        assert self.app.last_job is not None
        self.assertIn("Deleted 1 backup", self.app.last_job[0])
        self.assertNotIn("backup:aaaa0001", self._state_text())  # rescanned afterwards

    def test_protected_remote_and_unknown_backups_cannot_be_selected(self) -> None:
        self.app.run_scan()
        for slug in ("bbbb0001", "cccc0001", "zzzz9999", "../addons", ""):
            with self.subTest(slug=slug), self.assertRaises(webui.PanelError):
                self.app.panel_toggle(f"backup:{slug}", True)
        self.assertEqual(self.app.selected, set())

    def test_a_selected_backup_that_became_protected_is_not_deleted(self) -> None:
        self.app.run_scan()
        self.app.panel_toggle("backup:aaaa0002", True)
        for row in self.sup.backups:
            if row["slug"] == "aaaa0002":
                row["protected"] = True
                row["location_attributes"] = {".local": {"protected": True, "size_bytes": GB}}
        self._action("delete_backups")
        self._drain()
        self.assertEqual(self.sup.deleted(), [])
        assert self.app.last_job is not None
        self.assertEqual(self.app.last_job[1], "error")

    def test_clean_files_button(self) -> None:
        self.app.run_scan()
        self._action("clean_files")
        self._drain()
        assert self.app.last_job is not None
        self.assertEqual(self.app.last_job, ("Freed 1.0 KB from 2 files", "ok"))
        self.assertEqual((self.root / database.DB_NAME).stat().st_size, 5000)

    def test_repack_reports_the_watched_size_even_when_the_call_times_out(self) -> None:
        self.sup.purge_error = "POST /core/api/services/recorder/purge failed: timed out"
        self.app.run_scan()
        self._action("repack")
        self._drain()
        self.assertEqual(self.sup.purges(), [{"repack": True}])
        assert self.app.last_job is not None
        self.assertIn("has not answered within 5 minutes", self.app.last_job[0])
        watch = self.app.watch
        assert watch is not None
        with self.assertRaises(webui.PanelError):
            self._action("purge")  # one at a time
        self.assertIn("watching the database size", self._state_text())
        (self.root / database.DB_NAME).write_bytes(b"d" * 2000)
        self.app.poll_watch(watch.next_poll)
        self.assertIsNotNone(self.app.watch)
        self.app.poll_watch(watch.last_change + database.SETTLE_SECONDS + 1)
        self.assertIsNone(self.app.watch)
        assert self.app.last_job is not None
        self.assertEqual(self.app.last_job[1], "ok")
        self.assertIn("5.0 KB to 2.0 KB", self.app.last_job[0])

    def test_a_refused_purge_is_shown_and_not_watched(self) -> None:
        self.sup.purge_error = "HTTP 400 from POST /core/api/services/recorder/purge"
        self._action("purge")
        self._drain()
        self.assertIsNone(self.app.watch)
        assert self.app.last_job is not None
        self.assertEqual(self.app.last_job[1], "error")

    def test_scan_button_queues_one_scan(self) -> None:
        self.assertEqual(self._action("scan"), "Scanning...")
        self.assertEqual(self._action("scan"), "A scan is already waiting")
        self._drain()
        self.assertIsNotNone(self.app.scan)
        with self.assertRaises(webui.PanelError):
            self._action("format_disk")

    # -- options and notifications -----------------------------------------------------------------
    def test_switches_are_saved_and_applied(self) -> None:
        self.app.panel_toggle("auto_clean", True)
        self.assertTrue(self.sup.written[-1]["auto_clean"])
        self.assertFalse(self.sup.written[-1]["auto_clean_backups"])
        self.assertTrue(self.app.view_config().auto_clean)
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("keep_backups", True)

    def test_a_switch_without_the_supervisor_writes_nothing(self) -> None:
        self.sup.reachable = False
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("auto_clean", True)
        self.assertEqual(self.sup.written, [])

    def test_secret_references_are_written_back_unchanged(self) -> None:
        stored: dict[str, object] = {**STORED, "log_level": "!secret level"}
        self.sup.stored = copy.deepcopy(stored)
        secrets = webui.SecretRefs()
        raw = webui.initial_options(lambda: {**STORED, "log_level": "debug"}, self.sup.fetch_options, secrets)
        app = main.App(config.load_config(secrets.resolve_options(raw)), self.sup, self.store, raw, ha_root=self.root)
        app.secrets = secrets
        self.assertEqual(app.config.log_level, "debug")
        app.panel_toggle("notify_on_low_space", False)
        self.assertEqual(self.sup.written[-1]["log_level"], "!secret level")

    def test_options_edited_in_the_configuration_tab_are_picked_up(self) -> None:
        self.sup.stored["keep_backups"] = 7
        self.app.check_options()
        self.assertEqual(self.app.config.keep_backups, 7)
        self.sup.stored["keep_backups"] = 0
        self.app.check_options()
        self.assertEqual(self.app.config.keep_backups, 7)

    def test_low_space_is_notified_once_and_again_after_it_recovered(self) -> None:
        self.app.refresh_phones()
        self.sup.usage = usage_reply(used=31 * GB)
        self.app.run_scan()
        self.app.run_scan()
        self.assertEqual(len(self.sup.sent), 1)
        self.assertIn("Low space", self._state_text())
        self.sup.usage = usage_reply(used=10 * GB)
        self.app.run_scan()
        self.sup.usage = usage_reply(used=31 * GB)
        self.app.run_scan()
        self.assertEqual(len(self.sup.sent), 2)

    def test_phone_switch(self) -> None:
        self.app.refresh_phones()
        self.app.panel_toggle("phone:mobile_app_example_phone", False)
        self.assertIs(self.app.phones["mobile_app_example_phone"], False)
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("phone:mobile_app_gone", False)

    def test_the_loop_scans_on_its_first_pass(self) -> None:
        self.app.next_options_check = time.monotonic() + 1000
        self.app._tick()
        self._drain()
        self.assertIsNotNone(self.app.scan)


if __name__ == "__main__":
    unittest.main()
