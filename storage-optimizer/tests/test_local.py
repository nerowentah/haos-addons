"""Tests for the only code that changes files in the configuration folder: what it cleans, and what it refuses."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import local


class LocalTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "homeassistant"
        self.root.mkdir()
        self.outside = self.base / "outside.log"
        self.outside.write_text("keep me", encoding="utf-8")

    def _write(self, rel: str, text: str = "x" * 100) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _link(self, link: Path, target: Path, *, directory: bool = False) -> None:
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as err:
            self.skipTest(f"this system cannot make symbolic links: {err}")

    # -- what is cleaned ----------------------------------------------------------------
    def test_scan_lists_only_the_known_files(self) -> None:
        self._write("home-assistant.log")
        self._write("home-assistant.log.1")
        self._write("tts/abc_en_-_demo.mp3")
        self._write("tts/notes.txt")
        self._write("configuration.yaml")
        self._write("home-assistant_v2.db")
        self._write(".storage/auth")
        self._write("deps/lib.py")
        self._write("custom_components/example/__init__.py")
        found = {item.rel: item.kind for item in local.scan(self.root)}
        self.assertEqual(
            found,
            {
                "home-assistant.log": local.TRUNCATE,
                "home-assistant.log.1": local.DELETE,
                "tts/abc_en_-_demo.mp3": local.DELETE,
            },
        )

    def test_an_installation_without_a_log_file_has_nothing_to_clean(self) -> None:
        self._write("configuration.yaml")
        self.assertEqual(local.scan(self.root), [])
        self.assertEqual(local.clean(self.root).cleaned, [])

    def test_clean_empties_the_live_log_and_deletes_the_rest(self) -> None:
        live = self._write("home-assistant.log", "a" * 500)
        old = self._write("home-assistant.log.1", "b" * 300)
        cached = self._write("tts/abc_en_-_demo.mp3", "c" * 200)
        keep = [
            self._write("configuration.yaml"),
            self._write("home-assistant_v2.db"),
            self._write("home-assistant_v2.db-wal"),
            self._write(".storage/auth"),
            self._write("deps/lib.py"),
            self._write("custom_components/example/__init__.py"),
            self._write("tts/notes.txt"),
        ]
        result = local.clean(self.root)
        self.assertEqual(result.freed, 1000)
        self.assertEqual(result.errors, [])
        self.assertEqual(
            sorted(result.cleaned), ["home-assistant.log", "home-assistant.log.1", "tts/abc_en_-_demo.mp3"]
        )
        self.assertTrue(live.exists())
        self.assertEqual(live.stat().st_size, 0)
        self.assertFalse(old.exists())
        self.assertFalse(cached.exists())
        for path in keep:
            with self.subTest(path=path.name):
                self.assertEqual(path.stat().st_size, 100)

    def test_summary_groups_by_kind(self) -> None:
        self._write("home-assistant.log", "a" * 10)
        self._write("tts/one.mp3", "b" * 20)
        self._write("tts/two.mp3", "c" * 30)
        self.assertEqual(
            local.summary(local.scan(self.root)), [("Home Assistant log", 1, 10), ("Text-to-speech cache", 2, 50)]
        )

    # -- what is refused -------------------------------------------------------------------
    def test_names_off_the_list_and_escapes_are_refused(self) -> None:
        self._write("configuration.yaml")
        self._write("home-assistant.log")
        self._write("tts/a.mp3")
        self._write("tts/deeper/a.mp3")
        for rel in (
            "",
            "configuration.yaml",
            "../outside.log",
            "tts/../../outside.log",
            "tts/../home-assistant.log",
            "./home-assistant.log",
            "/etc/passwd",
            "tts\\a.mp3",
            "tts//a.mp3",
            "tts/deeper/a.mp3",
            "media/a.mp3",
            "tts",
        ):
            with self.subTest(rel=rel), self.assertRaises(local.RefusedError):
                local.check(self.root, rel)
        self.assertEqual(local.check(self.root, "tts/a.mp3").name, "a.mp3")

    def test_never_touched_names_are_refused_even_inside_a_cache_folder(self) -> None:
        for rel in (
            "home-assistant_v2.db",
            "home-assistant_v2.db-wal",
            "tts/copy.db.mp3",
            ".storage/auth",
            "deps/a.mp3",
            "custom_components/a.mp3",
        ):
            self._write(rel)
            with self.subTest(rel=rel), self.assertRaises(local.RefusedError):
                local.check(self.root, rel)

    def test_a_missing_file_and_a_folder_are_refused(self) -> None:
        with self.assertRaises(local.RefusedError):
            local.check(self.root, "home-assistant.log")
        (self.root / "home-assistant.log.1").mkdir()
        with self.assertRaises(local.RefusedError):
            local.check(self.root, "home-assistant.log.1")
        self.assertEqual(local.scan(self.root), [])

    def test_a_linked_log_is_left_alone(self) -> None:
        self._link(self.root / "home-assistant.log", self.outside)
        with self.assertRaises(local.RefusedError):
            local.check(self.root, "home-assistant.log")
        self.assertEqual(local.scan(self.root), [])
        self.assertEqual(local.clean(self.root).cleaned, [])
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "keep me")

    def test_a_linked_cache_folder_is_left_alone(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        victim = elsewhere / "song.mp3"
        victim.write_text("keep me", encoding="utf-8")
        self._link(self.root / "tts", elsewhere, directory=True)
        with self.assertRaises(local.RefusedError):
            local.check(self.root, "tts/song.mp3")
        self.assertEqual(local.scan(self.root), [])
        self.assertEqual(local.clean(self.root).cleaned, [])
        self.assertTrue(victim.exists())

    def test_a_link_inside_the_cache_folder_is_left_alone(self) -> None:
        (self.root / "tts").mkdir()
        self._link(self.root / "tts" / "song.mp3", self.outside)
        self.assertEqual(local.scan(self.root), [])
        self.assertEqual(local.clean(self.root).cleaned, [])
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "keep me")

    def test_a_linked_root_is_refused(self) -> None:
        real = self.base / "real"
        real.mkdir()
        (real / "home-assistant.log").write_text("keep me", encoding="utf-8")
        link = self.base / "linked"
        self._link(link, real, directory=True)
        with self.assertRaises(local.RefusedError):
            local.check(link, "home-assistant.log")
        self.assertEqual(local.clean(link).cleaned, [])
        self.assertEqual((real / "home-assistant.log").read_text(encoding="utf-8"), "keep me")

    def test_a_missing_root_is_not_an_error(self) -> None:
        missing = self.base / "nowhere"
        self.assertFalse(local.exists(missing))
        self.assertEqual(local.scan(missing), [])
        self.assertEqual(local.clean(missing).cleaned, [])


if __name__ == "__main__":
    unittest.main()
