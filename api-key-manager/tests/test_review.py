"""Tests for flags, review state, pruning and new-key detection (placeholder data)."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import auth_store
import review

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "auth.json"
NOW = datetime(2026, 11, 9, tzinfo=UTC)


def _key(name: str = "Node-RED", created_days: int = 30, used_days: int | None = 1) -> auth_store.Key:
    created = NOW - timedelta(days=created_days)
    return auth_store.Key(
        key_id="k1",
        user_id="u1",
        kind=auth_store.KIND_LONG_LIVED,
        name=name,
        client_id="",
        created_at=created,
        last_used_at=None if used_days is None else NOW - timedelta(days=used_days),
        last_used_ip="192.0.2.1",
        expires_at=created + timedelta(days=3650),
    )


USER = auth_store.User("u1", "Sam Sample", "sam", False, False, False, True, False)


class FlagTest(unittest.TestCase):
    def test_vague_descriptions(self) -> None:
        for text in ("", "abc", "test", "Test Token 2", "API key", "my-new-token"):
            self.assertTrue(review.is_vague(text), text)
        for text in ("Node-RED", "Grafana dashboard", "Backup script on NAS", "test grafana"):
            self.assertFalse(review.is_vague(text), text)
        self.assertTrue(review.is_vague("homelab token", ["homelab"]))

    def test_used_recently_has_no_flags(self) -> None:
        self.assertEqual(review.key_flags(_key(), USER, NOW, 90), [])

    def test_unused_and_never_used(self) -> None:
        self.assertEqual(review.key_flags(_key(created_days=400, used_days=200), USER, NOW, 90), [review.FLAG_UNUSED])
        self.assertEqual(review.key_flags(_key(used_days=None), USER, NOW, 90), [review.FLAG_NEVER_USED])
        fresh = _key(created_days=0, used_days=None)
        self.assertEqual(review.key_flags(fresh, USER, NOW, 90), [])  # one day of grace

    def test_creation_counts_as_never_used(self) -> None:
        key = _key(created_days=10, used_days=10)  # HA logs the creation as a use
        self.assertEqual(review.key_flags(key, USER, NOW, 90), [review.FLAG_NEVER_USED])

    def test_fixture_flags(self) -> None:
        snapshot = auth_store.parse(FIXTURE.read_text(encoding="utf-8"))
        flags = review.all_flags(snapshot, NOW, 90)
        self.assertEqual(flags["llat-node-red"], [review.FLAG_ADMIN])
        self.assertEqual(flags["llat-test"], [review.FLAG_NEVER_USED, review.FLAG_VAGUE, review.FLAG_ADMIN])
        self.assertEqual(flags["llat-grafana"], [review.FLAG_UNUSED])
        self.assertEqual(flags["llat-old"], [review.FLAG_INACTIVE_OWNER])
        self.assertNotIn("session-browser", flags)

    def test_duplicates_within_one_user(self) -> None:
        key = _key("Grafana")
        self.assertIn(review.FLAG_DUPLICATE, review.key_flags(key, USER, NOW, 90, duplicates={"grafana"}))


class ReviewStateTest(unittest.TestCase):
    def test_state_and_note_round_trip(self) -> None:
        reviews: dict[str, object] = {}
        review.set_state(reviews, "k1", review.STATE_APPROVED, NOW)
        self.assertEqual(review.set_note(reviews, "k1", "  used by\n Grafana  "), "used by Grafana")
        self.assertEqual(review.review_of(reviews, "k1"), review.Review("approved", "used by Grafana", NOW))
        self.assertEqual(
            reviews["k1"],  # the saved form is unchanged
            {"state": "approved", "note": "used by Grafana", "reviewed_at": "2026-11-09T00:00:00+00:00"},
        )
        review.set_state(reviews, "k1", review.STATE_UNREVIEWED)
        review.set_note(reviews, "k1", "")
        self.assertEqual(reviews, {})  # nothing left to keep

    def test_unknown_state_is_rejected(self) -> None:
        self.assertIsNone(review.parse_state("maybe"))
        self.assertIsNone(review.parse_state(["approved"]))
        self.assertEqual(review.parse_state("approved"), review.STATE_APPROVED)
        self.assertEqual(review.review_of({"k1": {"state": "maybe"}}, "k1").state, review.STATE_UNREVIEWED)

    def test_malformed_review_time_is_dropped(self) -> None:
        entry = review.review_of({"k1": {"state": "approved", "reviewed_at": "yesterday"}}, "k1")
        self.assertEqual((entry.state, entry.reviewed_at), (review.STATE_APPROVED, None))
        naive = review.review_of({"k1": {"state": "approved", "reviewed_at": "2026-11-09T00:00:00"}}, "k1")
        self.assertEqual(naive.reviewed_at, NOW)

    def test_note_is_capped(self) -> None:
        self.assertEqual(len(review.set_note({}, "k1", "x" * 2000)), review.NOTE_MAX_CHARS)

    def test_prune_and_new_keys(self) -> None:
        snapshot = auth_store.parse(FIXTURE.read_text(encoding="utf-8"))
        seen: dict[str, object] = {}
        self.assertEqual(review.new_keys(seen, snapshot, first_run=True), [])
        self.assertEqual(len(seen), 4)
        self.assertEqual(review.new_keys(seen, snapshot, first_run=False), [])
        seen.pop("llat-grafana")
        self.assertEqual([k.key_id for k in review.new_keys(seen, snapshot, first_run=False)], ["llat-grafana"])
        reviews: dict[str, object] = {"llat-grafana": {"state": "approved"}, "deleted": {"state": "approved"}}
        self.assertTrue(review.prune(reviews, {"llat-grafana"}))
        self.assertEqual(list(reviews), ["llat-grafana"])


if __name__ == "__main__":
    unittest.main()
