"""Tests for the app: options with !secret references, panel actions, and no secrets in the panel (no network)."""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import auth_store
import config
import main
import store
import supervisor
import webui

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "auth.json"
STORED = {"unused_days": 90, "notify_on_new_keys": True, "vague_words": ["!secret word"]}
RESOLVED = {**STORED, "vague_words": ["homelab"]}


class FakeSupervisor(supervisor.Supervisor):
    def __init__(self) -> None:
        super().__init__("token")
        self.stored = copy.deepcopy(STORED)
        self.reachable = True
        self.written: list[dict[str, object]] = []
        self.sent: list[tuple[str, str]] = []

    def fetch_options(self) -> dict[str, object]:
        if not self.reachable:
            raise supervisor.SupervisorError("unreachable")
        return copy.deepcopy(self.stored)

    def write_options(self, options: dict[str, object]) -> None:
        self.written.append(copy.deepcopy(options))

    def notify(self, targets: list[str], title: str, message: str, notification_id: str) -> None:  # noqa: ARG002
        self.sent.append((title, message))


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.auth = self.dir / "auth"
        shutil.copy(FIXTURE, self.auth)
        self.sup = FakeSupervisor()
        secrets = webui.SecretRefs()
        raw = webui.initial_options(lambda: copy.deepcopy(RESOLVED), self.sup.fetch_options, secrets)
        self.store = store.Store(self.dir / "state.json")
        self.app = main.App(
            config.load_config(secrets.resolve(raw)),
            self.sup,  # type: ignore[arg-type]
            self.store,
            raw,
            auth_store.AuthReader(self.auth),
        )
        self.app.secrets = secrets
        self.app.read()

    def _state_text(self) -> str:
        return json.dumps(self.app.panel_state())

    # -- Options ---------------------------------------------------------------------
    def test_live_change_resolves_references(self) -> None:
        self.sup.stored["unused_days"] = 30
        self.app.check_options()
        self.assertEqual(self.app.config.unused_days, 30)
        self.assertEqual(self.app.config.vague_words, ["homelab"])

    def test_panel_change_writes_references(self) -> None:
        self.app.panel_toggle("notify_on_new_keys", False)
        self.assertEqual(self.sup.written[-1]["vague_words"], ["!secret word"])

    def test_panel_change_without_the_supervisor_writes_nothing(self) -> None:
        self.sup.reachable = False
        with self.assertRaises(webui.PanelError):
            self.app.panel_toggle("notify_on_new_keys", False)
        self.assertEqual(self.sup.written, [])

    # -- Panel ------------------------------------------------------------------------
    def test_panel_never_shows_secrets_or_system_users(self) -> None:
        text = self._state_text()
        self.assertNotIn("PLACEHOLDER-SECRET", text)
        self.assertNotIn("PLACEHOLDER-HASH", text)
        self.assertNotIn("Supervisor", text)
        for name in ("Node-RED on the NAS", "test token", "Grafana", "Backup script"):
            self.assertIn(name, text)

    def test_review_and_note_persist(self) -> None:
        self.app.panel_action("review", {"key": "llat-grafana", "state": "approved"})
        self.app.panel_action("note", {"key": "llat-grafana", "text": "Dashboards on the NAS"})
        self.store.save()
        saved = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["reviews"]["llat-grafana"]["state"], "approved")
        self.assertEqual(saved["reviews"]["llat-grafana"]["note"], "Dashboards on the NAS")
        self.assertIn("Dashboards on the NAS", self._state_text())
        self.assertNotIn("PLACEHOLDER-SECRET", (self.dir / "state.json").read_text(encoding="utf-8"))

    def test_hiding_approved_keys(self) -> None:
        self.app.panel_action("review", {"key": "llat-grafana", "state": "approved"})
        self.app.panel_toggle("view:show_approved", False)
        text = self._state_text()
        self.assertNotIn('"title": "Grafana"', text)
        self.assertIn("1 approved key(s) hidden", text)

    def test_sessions_only_when_switched_on(self) -> None:
        self.assertNotIn("homeassistant.local", self._state_text())
        self.app.panel_toggle("view:show_sessions", True)
        self.assertIn("homeassistant.local", self._state_text())

    def test_unknown_key_and_state_are_refused(self) -> None:
        with self.assertRaises(webui.PanelError):
            self.app.panel_action("review", {"key": "nope", "state": "approved"})
        with self.assertRaises(webui.PanelError):
            self.app.panel_action("review", {"key": "llat-grafana", "state": "maybe"})
        with self.assertRaises(webui.PanelError):
            self.app.panel_action("review", {"key": "session-browser", "state": "approved"})

    def test_revoke_help_names_the_owner(self) -> None:
        self.assertIn("Sam Sample", self.app.panel_action("revoke_help", {"key": "llat-grafana"}) or "")

    # -- New keys and deleted keys --------------------------------------------------
    def test_new_key_is_notified_once_and_deleted_key_pruned(self) -> None:
        self.assertEqual(self.sup.sent, [])  # nothing is new on the first read
        self.app.panel_action("review", {"key": "llat-old", "state": "suspicious"})
        doc = json.loads(self.auth.read_text(encoding="utf-8"))
        tokens = doc["data"]["refresh_tokens"]
        added = dict(tokens[0], id="llat-new", client_name="Home Assistant Assist satellite")
        doc["data"]["refresh_tokens"] = [t for t in tokens if t["id"] != "llat-old"] + [added]
        self.auth.write_text(json.dumps(doc), encoding="utf-8")
        self.app.reader.force()
        self.app.read()
        self.assertEqual(len(self.sup.sent), 1)
        self.assertIn("Home Assistant Assist satellite", self.sup.sent[0][1])
        self.assertNotIn("llat-old", self.store.section("reviews"))
        self.app.reader.force()
        self.app.read()
        self.assertEqual(len(self.sup.sent), 1)

    def test_read_failures_notify_after_three(self) -> None:
        self.auth.unlink()
        for _ in range(4):
            self.app.read()
        self.assertEqual(len(self.sup.sent), 1)
        self.assertIn("Read failing", self._state_text())

    def test_unused_days_is_clamped(self) -> None:
        self.assertEqual(config.load_config({"unused_days": 1}).unused_days, 7)


if __name__ == "__main__":
    unittest.main()
