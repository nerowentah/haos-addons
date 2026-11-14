"""Review state and automatic flags for API keys (pure logic, no I/O)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from auth_store import KIND_LONG_LIVED, Key, Snapshot, User

type State = Literal["unreviewed", "approved", "suspicious"]
STATE_UNREVIEWED: Final = "unreviewed"
STATE_APPROVED: Final = "approved"
STATE_SUSPICIOUS: Final = "suspicious"
STATES: tuple[State, ...] = (STATE_UNREVIEWED, STATE_APPROVED, STATE_SUSPICIOUS)

type Flag = Literal[
    "inactive_owner",
    "expired",
    "never_used",
    "unused",
    "vague_description",
    "duplicate_description",
    "admin_rights",
]
FLAG_INACTIVE_OWNER: Final = "inactive_owner"
FLAG_EXPIRED: Final = "expired"
FLAG_NEVER_USED: Final = "never_used"
FLAG_UNUSED: Final = "unused"
FLAG_VAGUE: Final = "vague_description"
FLAG_DUPLICATE: Final = "duplicate_description"
FLAG_ADMIN: Final = "admin_rights"
# Most serious first: the card badge shows the first flag that applies.
FLAG_ORDER: tuple[Flag, ...] = (
    FLAG_INACTIVE_OWNER,
    FLAG_EXPIRED,
    FLAG_NEVER_USED,
    FLAG_UNUSED,
    FLAG_VAGUE,
    FLAG_DUPLICATE,
    FLAG_ADMIN,
)
# Flags that ask for attention (admin rights alone is informational).
ATTENTION_FLAGS = frozenset(FLAG_ORDER) - {FLAG_ADMIN}

VAGUE_WORDS = frozenset(
    {"a", "access", "api", "asdf", "foo", "ha", "hass", "key", "long", "lived", "my", "new", "qwerty", "temp",
     "test", "testing", "tmp", "token", "x", "xxx"}
)  # fmt: skip
MIN_DESCRIPTION_CHARS = 4
NEVER_USED_GRACE = timedelta(days=1)
NOTE_MAX_CHARS = 500


def is_vague(description: str, extra_words: Collection[str] = ()) -> bool:
    """True when a description says nothing about who or what uses the key.

    Empty, shorter than 4 characters, or made only of generic words ("test token", "API key 2").
    """

    text = description.strip().lower()
    if len(text) < MIN_DESCRIPTION_CHARS:
        return True
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w and not w.isdigit()]
    vague = VAGUE_WORDS | {w.lower() for w in extra_words}
    return all(w in vague for w in words)


def key_flags(  # noqa: PLR0913 - every input a flag needs
    key: Key,
    user: User,
    now: datetime,
    unused_days: int,
    *,
    duplicates: set[str] | None = None,
    extra_vague: Collection[str] = (),
) -> list[Flag]:
    """Flags for one long-lived key, most serious first."""

    flags: set[Flag] = set()
    if not user.is_active:
        flags.add(FLAG_INACTIVE_OWNER)
    if key.expires_at is not None and key.expires_at <= now:
        flags.add(FLAG_EXPIRED)
    used = key.last_used_at
    # A new key's "last used" is its creation moment; treat that as not yet used.
    if used is None or (key.created_at is not None and used - key.created_at < timedelta(seconds=5)):
        if key.created_at is None or now - key.created_at > NEVER_USED_GRACE:
            flags.add(FLAG_NEVER_USED)
    elif now - used > timedelta(days=unused_days):
        flags.add(FLAG_UNUSED)
    if is_vague(key.name, extra_vague):
        flags.add(FLAG_VAGUE)
    if duplicates and key.name.strip().lower() in duplicates:
        flags.add(FLAG_DUPLICATE)
    if user.is_owner or user.is_admin:
        flags.add(FLAG_ADMIN)
    return [f for f in FLAG_ORDER if f in flags]


def all_flags(
    snapshot: Snapshot, now: datetime, unused_days: int, extra_vague: Collection[str] = ()
) -> dict[str, list[Flag]]:
    """Flags for every long-lived key, by key id."""

    users = {u.user_id: u for u in snapshot.users}
    result: dict[str, list[Flag]] = {}
    for user in snapshot.users:
        keys = snapshot.keys_of(user.user_id, KIND_LONG_LIVED)
        counts = Counter(k.name.strip().lower() for k in keys if k.name.strip())
        duplicates = {name for name, n in counts.items() if n > 1}
        for key in keys:
            result[key.key_id] = key_flags(
                key, users[key.user_id], now, unused_days, duplicates=duplicates, extra_vague=extra_vague
            )
    return result


def parse_state(value: object) -> State | None:
    """``value`` as a review state; None when it is not one."""

    for state in STATES:
        if state == value:
            return state
    return None


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Review:
    """What the user decided about one key, as kept in /data."""

    state: State = STATE_UNREVIEWED
    note: str = ""
    reviewed_at: datetime | None = None

    @classmethod
    def from_json(cls, entry: object) -> Review:
        """The stored entry ({state, note, reviewed_at}); unreviewed when it is missing or malformed."""

        if not isinstance(entry, dict):
            return cls()
        return cls(
            state=parse_state(entry.get("state")) or STATE_UNREVIEWED,
            note=str(entry.get("note") or ""),
            reviewed_at=_moment(entry.get("reviewed_at")),
        )

    def to_json(self) -> dict[str, str]:
        """The entry to store."""

        reviewed = self.reviewed_at.isoformat(timespec="seconds") if self.reviewed_at else ""
        return {"state": self.state, "note": self.note, "reviewed_at": reviewed}


def review_of(reviews: dict[str, object], key_id: str) -> Review:
    """The stored review for a key; unreviewed when missing or malformed."""

    return Review.from_json(reviews.get(key_id))


def set_state(reviews: dict[str, object], key_id: str, state: State, now: datetime | None = None) -> None:
    """Record a review decision (the note is kept)."""

    reviewed = None if state == STATE_UNREVIEWED else (now or datetime.now(UTC))
    _store(reviews, key_id, replace(review_of(reviews, key_id), state=state, reviewed_at=reviewed))


def set_note(reviews: dict[str, object], key_id: str, note: str) -> str:
    """Set (or clear) a key's note; returns the stored text (one line, at most 500 characters)."""

    text = " ".join(note.split())[:NOTE_MAX_CHARS]
    _store(reviews, key_id, replace(review_of(reviews, key_id), note=text))
    return text


def _store(reviews: dict[str, object], key_id: str, entry: Review) -> None:
    if entry.state == STATE_UNREVIEWED and not entry.note:
        reviews.pop(key_id, None)
    else:
        reviews[key_id] = entry.to_json()


def prune(section: dict[str, object], live_ids: set[str]) -> bool:
    """Forget entries for keys that no longer exist; True when something was removed."""

    gone = [k for k in section if k not in live_ids]
    for key_id in gone:
        del section[key_id]
    return bool(gone)


def new_keys(seen: dict[str, object], snapshot: Snapshot, *, first_run: bool) -> list[Key]:
    """Long-lived keys not seen before (marks them seen). Nothing counts as new on the very first read."""

    fresh = [k for k in snapshot.keys if k.kind == KIND_LONG_LIVED and k.key_id not in seen]
    for key in fresh:
        seen[key.key_id] = True
    return [] if first_run else fresh
