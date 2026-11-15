"""The only code that changes anything inside Home Assistant's configuration folder.

It may do exactly two things, to files on a fixed list:

* empty a log file Home Assistant keeps open (``home-assistant.log``);
* delete a rotated log or a cached file Home Assistant makes again when it needs it.

Everything else is refused, whatever asks for it: a name that is not on the
list, a symbolic link (the file itself or a folder on the way to it), a path
that ends up outside the folder, anything that is not a regular file, and
every name on the never-touch list (``.storage``, ``deps``,
``custom_components``, any database file). The checks are made again on the
open file, right before it is changed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_LOGGER = logging.getLogger(__name__)

ROOT = Path(os.environ.get("STORAGE_OPTIMIZER_HA_CONFIG", "/homeassistant"))

type Kind = Literal["truncate", "delete"]
TRUNCATE: Kind = "truncate"
DELETE: Kind = "delete"

# Files in the folder itself: name -> (what to do, what the panel calls it).
FILES: dict[str, tuple[Kind, str]] = {
    "home-assistant.log": (TRUNCATE, "Home Assistant log"),
    "home-assistant.log.1": (DELETE, "Previous Home Assistant log"),
    "home-assistant.log.old": (DELETE, "Previous Home Assistant log"),
    "home-assistant.log.fault": (TRUNCATE, "Crash log"),
    "OZW_Log.txt": (DELETE, "Old Z-Wave log"),
}
# Cache folders: name -> (file endings that may be deleted inside it, what the panel calls it).
CACHE_DIRS: dict[str, tuple[frozenset[str], str]] = {
    "tts": (frozenset({".mp3", ".wav", ".ogg", ".flac", ".opus"}), "Text-to-speech cache"),
}
NEVER_PARTS = frozenset({".storage", "deps", "custom_components", "blueprints", "www", "themes"})
NEVER_IN_NAME = ".db"  # home-assistant_v2.db, its -wal and -shm, zigbee.db, backups of them...
MAX_CACHE_FILES = 20000  # a folder with more is reported as it is and left alone beyond this


class RefusedError(ValueError):
    """The path is not one this app may change."""


@dataclass(frozen=True, slots=True)
class Item:
    """A file that may be cleaned: where it is (relative to the folder), its size and what happens to it."""

    rel: str
    size: int
    kind: Kind
    label: str


@dataclass(slots=True)
class Result:
    """What a clean-up did."""

    freed: int = 0
    cleaned: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0))  # Windows junctions too


def check(root: Path, rel: str) -> Path:
    """The file ``rel`` names inside ``root``, when the app may change it; RefusedError otherwise."""

    parts = rel.split("/")
    if not rel or "\\" in rel or any(part in ("", ".", "..") for part in parts):
        raise RefusedError(f"{rel!r} is not a plain path inside the folder")
    if any(part in NEVER_PARTS for part in parts) or NEVER_IN_NAME in parts[-1].lower():
        raise RefusedError(f"{rel} is never touched")
    if len(parts) == 1:
        allowed = parts[0] in FILES
    else:
        cache = CACHE_DIRS.get(parts[0])
        allowed = cache is not None and len(parts) == 2 and Path(parts[1]).suffix.lower() in cache[0]  # noqa: PLR2004 - folder/file
    if not allowed:
        raise RefusedError(f"{rel} is not on the list of files this app cleans")
    try:
        if _is_link(root.lstat()):
            raise RefusedError(f"{root} is a link")
        base = root.resolve(strict=True)
        path = base
        for part in parts:
            path = path / part
            info = path.lstat()
            if _is_link(info):
                raise RefusedError(f"{rel} is, or is behind, a link")
        if not stat.S_ISREG(info.st_mode):
            raise RefusedError(f"{rel} is not a regular file")
        if path.resolve(strict=True).parent != base.joinpath(*parts[:-1]):
            raise RefusedError(f"{rel} leads outside the folder")
    except OSError as err:
        raise RefusedError(f"{rel} cannot be checked: {err}") from err
    return path


def _item(root: Path, rel: str, kind: Kind, label: str) -> Item | None:
    try:
        path = check(root, rel)
        return Item(rel, path.lstat().st_size, kind, label)
    except (RefusedError, OSError):
        return None


def scan(root: Path = ROOT) -> list[Item]:
    """Every file that could be cleaned now (cache folders as one entry per file), in list order."""

    items = [item for name, (kind, label) in FILES.items() if (item := _item(root, name, kind, label))]
    for folder, (_endings, label) in CACHE_DIRS.items():
        directory = root / folder
        try:
            if _is_link(directory.lstat()) or not directory.is_dir():
                continue
            names = sorted(entry.name for entry in os.scandir(directory))[:MAX_CACHE_FILES]
        except OSError:
            continue
        items += [item for name in names if (item := _item(root, f"{folder}/{name}", DELETE, label))]
    return items


def _truncate(path: Path) -> None:
    """Empty the file through a descriptor that is checked again once it is open."""

    fd = os.open(path, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RefusedError(f"{path.name} is not a regular file")
        os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def clean(root: Path = ROOT) -> Result:
    """Empty or delete every file :func:`scan` lists. A file that cannot be cleaned is reported, not forced."""

    result = Result()
    for item in scan(root):
        if item.kind == TRUNCATE and item.size == 0:
            continue
        try:
            path = check(root, item.rel)  # again: the folder may have changed since the scan
            size = path.lstat().st_size
            if item.kind == TRUNCATE:
                _truncate(path)
            else:
                path.unlink()
        except (RefusedError, OSError) as err:
            _LOGGER.warning("Not cleaned: %s (%s)", item.rel, err)
            result.errors.append(f"{item.rel}: {err}")
            continue
        result.freed += size
        result.cleaned.append(item.rel)
    return result


def summary(items: list[Item]) -> list[tuple[str, int, int]]:
    """(label, files, bytes) per kind of file, in first-seen order, for the panel."""

    totals: dict[str, tuple[int, int]] = {}
    for item in items:
        count, size = totals.get(item.label, (0, 0))
        totals[item.label] = (count + 1, size + item.size)
    return [(label, count, size) for label, (count, size) in totals.items()]


def exists(root: Path = ROOT) -> bool:
    """Whether the configuration folder is mapped into the app at all."""

    with contextlib.suppress(OSError):
        return root.is_dir()
    return False
