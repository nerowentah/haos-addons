"""Every app ships the shared modules exactly as they are in shared/app."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import sync_shared

REPO = sync_shared.REPO
_ICON_NAME = re.compile(r"""["']mdi:([a-z0-9-]+)["']""")
_ICON_ENTRY = re.compile(r'^  "([a-z0-9-]+)": "', re.MULTILINE)


class SyncTest(unittest.TestCase):
    def test_every_copy_matches_its_source(self) -> None:
        stale = [path.relative_to(REPO).as_posix() for path in sync_shared.differing()]
        self.assertEqual(stale, [], "run: python tools/sync_shared.py")

    def test_every_shared_file_is_listed(self) -> None:
        self.assertEqual(sync_shared.unlisted(), [])

    def test_codeql_skips_exactly_the_copies(self) -> None:
        config = (REPO / ".github" / "codeql" / "codeql-config.yml").read_text(encoding="utf-8")
        ignored = re.findall(r'^  - "\*\*/rootfs/app/(.+)"$', config, re.MULTILINE)
        self.assertEqual(sorted(ignored), sorted(sync_shared.SHARED))

    def test_every_app_gets_the_panel(self) -> None:
        apps = sync_shared.app_names()
        self.assertGreaterEqual(len(apps), 7)
        for app in apps:
            for name in ("webui.py", "web/panel.js", "web/icons.js", "safefile.py"):
                with self.subTest(app=app, file=name):
                    self.assertTrue((REPO / app / "rootfs" / "app" / name).is_file())

    def test_every_icon_an_app_names_is_in_icons_js(self) -> None:
        known = set(_ICON_ENTRY.findall((sync_shared.SOURCE / "web" / "icons.js").read_text(encoding="utf-8")))
        self.assertGreater(len(known), 50)
        for app in sync_shared.app_names():
            used: set[str] = set()
            for path in (REPO / app / "rootfs" / "app").glob("*.py"):
                if path.name == "mqtt_pub.py":  # entity icons: drawn by Home Assistant, not by the panel
                    continue
                used.update(_ICON_NAME.findall(path.read_text(encoding="utf-8")))
            with self.subTest(app=app):
                self.assertEqual(sorted(used - known), [], "add the icon to shared/app/web/icons.js")


if __name__ == "__main__":
    unittest.main()
