"""Home Assistant backups, through the Supervisor API only (the backup folder is not mapped into the app).

The app only ever offers, and only ever deletes, a backup that

* has a copy on this system (the ``.local`` location), and
* is not password-protected there.

A delete asks the Supervisor for the list again and checks both once more,
so a stale panel or a made-up id cannot delete anything else. A backup that
also lives somewhere else (a network share, a cloud location) keeps that copy:
only the local one is removed.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from supervisor import Supervisor

LOCAL = ".local"
_SLUG_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MB = 1024 * 1024


class BackupError(ValueError):
    """A backup cannot be read or may not be deleted."""


@dataclass(frozen=True, slots=True)
class Backup:
    """One backup, as far as this system's copy of it is concerned."""

    slug: str
    name: str
    date: dt.datetime | None
    kind: str  # full | partial
    size: int  # bytes of the local copy (0 when unknown or not local)
    local: bool
    protected: bool
    elsewhere: tuple[str, ...]  # other locations that hold a copy

    @property
    def deletable(self) -> bool:
        """Local and not password-protected: the only backups this app deletes."""

        return self.local and not self.protected and bool(_SLUG_RE.fullmatch(self.slug))


def _date(value: object) -> dt.datetime | None:
    try:
        moment = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def _bytes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(int(value), 0)


def _parse_one(row: object) -> Backup | None:
    if not isinstance(row, dict) or not isinstance(row.get("slug"), str) or not row["slug"]:
        return None
    attributes = row.get("location_attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    locations = row.get("locations")
    listed = locations if isinstance(locations, list) else [row.get("location")] if "location" in row else []
    # The local copy is listed as null (older Supervisor) or ".local".
    names = {LOCAL if name in (None, "", LOCAL) else str(name) for name in listed} | {str(k) for k in attributes}
    local = LOCAL in names
    own = attributes.get(LOCAL)
    own = own if isinstance(own, dict) else {}
    protected = own.get("protected") if "protected" in own else row.get("protected")
    size = _bytes(own.get("size_bytes")) or _bytes(row.get("size_bytes"))
    legacy = row.get("size")  # older Supervisor: megabytes only
    if not size and isinstance(legacy, int | float) and not isinstance(legacy, bool):
        size = max(int(legacy * _MB), 0)
    return Backup(
        slug=row["slug"],
        name=str(row.get("name") or row["slug"]),
        date=_date(row.get("date")),
        kind=str(row.get("type") or ""),
        size=size if local else 0,
        local=local,
        protected=protected is not False,  # anything but a clear "not protected" counts as protected
        elsewhere=tuple(sorted(names - {LOCAL})),
    )


def parse(data: object) -> list[Backup]:
    """The backups in a ``GET /backups`` reply, newest first (undated ones last)."""

    rows = data.get("backups") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise BackupError("The Supervisor returned no list of backups")
    found = [backup for row in rows if (backup := _parse_one(row)) is not None]
    oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
    return sorted(found, key=lambda b: b.date or oldest, reverse=True)


def fetch(supervisor: Supervisor) -> list[Backup]:
    """Ask the Supervisor for the backups (raises SupervisorError or BackupError)."""

    return parse(supervisor.api_get("/backups"))


def select_auto(backups: list[Backup], keep: int) -> list[Backup]:
    """What the automatic clean-up deletes: deletable backups beyond the ``keep`` newest ones.

    A backup without a readable date is never chosen: its age is unknown.
    """

    if keep < 1:
        raise BackupError("At least one backup is always kept")
    dated = [backup for backup in backups if backup.deletable and backup.date is not None]
    dated.sort(key=lambda b: b.date or dt.datetime.min.replace(tzinfo=dt.UTC), reverse=True)
    return dated[keep:]


def delete(supervisor: Supervisor, slug: str) -> Backup:
    """Delete the local copy of one backup, after checking it against a fresh list.

    Raises BackupError when the id is unknown or the backup is protected or not local
    (and SupervisorError when the Supervisor refuses).
    """

    if not _SLUG_RE.fullmatch(slug):
        raise BackupError("That is not a backup id")
    current = next((backup for backup in fetch(supervisor) if backup.slug == slug), None)
    if current is None:
        raise BackupError("That backup no longer exists")
    if not current.local:
        raise BackupError(f"'{current.name}' is not stored on this system")
    if current.protected:
        raise BackupError(f"'{current.name}' is password-protected and is left alone")
    # Name the location only when there are other copies to keep: a plain delete removes every copy.
    supervisor.api_delete(f"/backups/{slug}", {"location": LOCAL} if current.elsewhere else None)
    return current
