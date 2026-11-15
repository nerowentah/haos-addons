"""Tests for the backups: which ones are offered, which the automatic clean-up picks, and what a delete refuses."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import backup
from helpers import GB, FakeSupervisor, backup_row, backup_rows


def _by_slug(rows: list[dict[str, object]]) -> dict[str, backup.Backup]:
    return {b.slug: b for b in backup.parse({"backups": rows})}


class ParseTest(unittest.TestCase):
    def test_newest_first_and_only_local_unprotected_are_deletable(self) -> None:
        found = backup.parse({"backups": backup_rows()})
        self.assertEqual(found[0].slug, "aaaa0004")
        self.assertEqual(
            sorted(b.slug for b in found if b.deletable), ["aaaa0001", "aaaa0002", "aaaa0003", "aaaa0004", "dddd0001"]
        )
        by_slug = {b.slug: b for b in found}
        self.assertTrue(by_slug["bbbb0001"].protected)
        self.assertFalse(by_slug["cccc0001"].local)
        self.assertEqual(by_slug["cccc0001"].size, 0)
        self.assertEqual(by_slug["dddd0001"].elsewhere, ("example_share",))
        self.assertEqual(by_slug["aaaa0001"].size, GB)

    def test_protection_is_read_from_the_local_copy(self) -> None:
        row = backup_row("eeee0001", "2026-01-01T00:00:00+00:00", locations=(".local", "example_share"))
        attributes = row["location_attributes"]
        assert isinstance(attributes, dict)
        attributes[".local"]["protected"] = True
        self.assertFalse(_by_slug([row])["eeee0001"].deletable)

    def test_an_unclear_protection_counts_as_protected(self) -> None:
        row = backup_row("eeee0002", "2026-01-01T00:00:00+00:00")
        del row["location_attributes"]
        row["protected"] = None
        self.assertFalse(_by_slug([row])["eeee0002"].deletable)

    def test_the_older_reply_without_location_attributes(self) -> None:
        row: dict[str, object] = {
            "slug": "ffff0001",
            "name": "Old style",
            "date": "2026-01-01T00:00:00",
            "type": "partial",
            "size": 10.5,
            "location": None,
            "protected": False,
        }
        found = _by_slug([row])["ffff0001"]
        self.assertTrue(found.deletable)
        self.assertEqual(found.size, int(10.5 * 1048576))
        self.assertIsNotNone(found.date)

    def test_rows_that_are_not_backups_are_skipped_and_a_bad_reply_is_refused(self) -> None:
        self.assertEqual(backup.parse({"backups": ["x", {}, {"slug": ""}, {"slug": 5}]}), [])
        for reply in (None, {}, {"backups": None}):
            with self.subTest(reply=reply), self.assertRaises(backup.BackupError):
                backup.parse(reply)

    def test_a_strange_id_is_never_deletable(self) -> None:
        row = backup_row("../addons", "2026-01-01T00:00:00+00:00")
        self.assertFalse(_by_slug([row])["../addons"].deletable)


class SelectTest(unittest.TestCase):
    def test_keeps_the_newest_and_never_picks_protected_or_remote(self) -> None:
        found = backup.parse({"backups": backup_rows()})
        self.assertEqual([b.slug for b in backup.select_auto(found, 2)], ["aaaa0002", "aaaa0001", "dddd0001"])
        self.assertEqual(backup.select_auto(found, 5), [])
        self.assertEqual(backup.select_auto(found, 50), [])

    def test_a_backup_without_a_date_is_never_picked(self) -> None:
        rows = [backup_row(f"aaaa000{n}", f"2026-01-0{n}T00:00:00+00:00") for n in (1, 2, 3)]
        rows.append(backup_row("aaaa0009", "not a date"))
        chosen = backup.select_auto(backup.parse({"backups": rows}), 1)
        self.assertEqual([b.slug for b in chosen], ["aaaa0002", "aaaa0001"])

    def test_keeping_nothing_is_refused(self) -> None:
        with self.assertRaises(backup.BackupError):
            backup.select_auto(backup.parse({"backups": backup_rows()}), 0)


class DeleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sup = FakeSupervisor()

    def test_a_local_backup_is_deleted(self) -> None:
        gone = backup.delete(self.sup, "aaaa0001")
        self.assertEqual(gone.slug, "aaaa0001")
        self.assertEqual(self.sup.deleted(), [("/backups/aaaa0001", None)])

    def test_only_the_local_copy_goes_when_there_is_another(self) -> None:
        backup.delete(self.sup, "dddd0001")
        self.assertEqual(self.sup.deleted(), [("/backups/dddd0001", {"location": ".local"})])

    def test_protected_remote_unknown_and_malformed_are_refused(self) -> None:
        for slug in ("bbbb0001", "cccc0001", "zzzz9999", "", "../addons", "aaaa0001/info", "aaaa0001?x=1"):
            with self.subTest(slug=slug), self.assertRaises(backup.BackupError):
                backup.delete(self.sup, slug)
        self.assertEqual(self.sup.deleted(), [])

    def test_a_backup_that_became_protected_since_the_panel_listed_it_is_refused(self) -> None:
        listed = backup.fetch(self.sup)
        self.assertTrue(next(b for b in listed if b.slug == "aaaa0002").deletable)
        self.sup.backups = [
            backup_row("aaaa0002", "2026-01-02T03:00:00+00:00", protected=True) if row["slug"] == "aaaa0002" else row
            for row in self.sup.backups
        ]
        with self.assertRaises(backup.BackupError):
            backup.delete(self.sup, "aaaa0002")
        self.assertEqual(self.sup.deleted(), [])


if __name__ == "__main__":
    unittest.main()
