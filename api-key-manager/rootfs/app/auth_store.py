"""Read-only view of Home Assistant's auth store (``/homeassistant/.storage/auth``).

Home Assistant has no API that lists other users' tokens, so the file is read
directly (mounted read-only). Only an allowlist of fields is kept: the token
secrets (``token``, ``jwt_key``) are dropped while the JSON is parsed, so they
never reach a Python object this app keeps, logs or shows.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

AUTH_PATH = Path(os.environ.get("AKM_AUTH_PATH", "/homeassistant/.storage/auth"))

KIND_LONG_LIVED = "long_lived"
KIND_SESSION = "session"
TOKEN_TYPES = {"long_lived_access_token": KIND_LONG_LIVED, "normal": KIND_SESSION}  # "system" is skipped
ADMIN_GROUP = "system-admin"
READ_ONLY_GROUP = "system-read-only"
SECRET_FIELDS = frozenset({"token", "jwt_key", "password"})


class AuthStoreError(RuntimeError):
    """Raised when the auth store cannot be read or has an unexpected shape."""


@dataclass(frozen=True, slots=True)
class User:
    """A person who can log in (system users are never listed)."""

    user_id: str
    name: str
    username: str
    is_owner: bool
    is_admin: bool
    is_read_only: bool
    is_active: bool
    local_only: bool

    @property
    def role(self) -> str:
        """Owner, Administrator, Read-only or User."""

        if self.is_owner:
            return "Owner"
        if self.is_admin:
            return "Administrator"
        return "Read-only" if self.is_read_only else "User"


@dataclass(frozen=True, slots=True)
class Key:
    """A long-lived access token (API key) or a login session (normal refresh token)."""

    key_id: str
    user_id: str
    kind: str
    name: str  # the description given when the token was created (client_name)
    client_id: str
    created_at: datetime | None
    last_used_at: datetime | None
    last_used_ip: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Users and their keys, as read from the auth store."""

    users: tuple[User, ...]
    keys: tuple[Key, ...]

    def keys_of(self, user_id: str, kind: str) -> list[Key]:
        """A user's keys of one kind, newest first."""

        found = [k for k in self.keys if k.user_id == user_id and k.kind == kind]
        oldest = datetime.min.replace(tzinfo=UTC)
        return sorted(found, key=lambda k: k.created_at or oldest, reverse=True)


def _drop_secrets(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``json`` object hook: token secrets are discarded as soon as they are parsed."""

    return {k: v for k, v in pairs if k not in SECRET_FIELDS}


def _time(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _list(data: dict[str, object], name: str) -> list[dict[str, object]]:
    rows = data.get(name)
    if not isinstance(rows, list):
        raise AuthStoreError(f"The auth store has no '{name}' list")
    return [r for r in rows if isinstance(r, dict)]


def parse(text: str) -> Snapshot:
    """Build a Snapshot from the auth store JSON (secrets dropped while parsing)."""

    try:
        doc = json.loads(text, object_pairs_hook=_drop_secrets)
    except ValueError as err:
        raise AuthStoreError(f"The auth store is not valid JSON: {err}") from None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        raise AuthStoreError("The auth store has no 'data' section")

    usernames: dict[str, str] = {}
    for cred in _list(data, "credentials"):
        details = cred.get("data")
        if isinstance(details, dict) and details.get("username"):
            usernames.setdefault(str(cred.get("user_id")), str(details["username"]))

    users: list[User] = []
    for raw in _list(data, "users"):
        if raw.get("system_generated"):
            continue
        groups = {str(g) for g in raw.get("group_ids") or [] if isinstance(g, str)}  # type: ignore[union-attr]
        user_id = str(raw.get("id") or "")
        if not user_id:
            continue
        users.append(
            User(
                user_id=user_id,
                name=str(raw.get("name") or usernames.get(user_id) or "(unnamed user)"),
                username=usernames.get(user_id, ""),
                is_owner=bool(raw.get("is_owner")),
                is_admin=ADMIN_GROUP in groups,
                is_read_only=READ_ONLY_GROUP in groups,
                is_active=bool(raw.get("is_active", True)),
                local_only=bool(raw.get("local_only")),
            )
        )
    known = {u.user_id for u in users}

    keys: list[Key] = []
    for raw in _list(data, "refresh_tokens"):
        kind = TOKEN_TYPES.get(str(raw.get("token_type") or ""))
        user_id = str(raw.get("user_id") or "")
        key_id = str(raw.get("id") or "")
        if kind is None or user_id not in known or not key_id:
            continue
        created = _time(raw.get("created_at"))
        if kind == KIND_LONG_LIVED:
            lifetime = raw.get("access_token_expiration")
            valid = isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool)
            expires = created + timedelta(seconds=float(lifetime)) if created and valid else None  # type: ignore[arg-type]
        else:
            expires = _time(raw.get("expire_at"))
        keys.append(
            Key(
                key_id=key_id,
                user_id=user_id,
                kind=kind,
                name=str(raw.get("client_name") or "").strip(),
                client_id=str(raw.get("client_id") or ""),
                created_at=created,
                last_used_at=_time(raw.get("last_used_at")),
                last_used_ip=str(raw.get("last_used_ip") or ""),
                expires_at=expires,
            )
        )

    def user_order(user: User) -> tuple[int, str]:
        rank = 0 if user.is_owner else 1 if user.is_admin else 2
        return (rank + (3 if not user.is_active else 0), user.name.lower())

    return Snapshot(users=tuple(sorted(users, key=user_order)), keys=tuple(keys))


class AuthReader:
    """Re-reads the auth store only when its modification time changes."""

    def __init__(self, path: Path = AUTH_PATH) -> None:
        self.path = path
        self._stamp: tuple[int, int] | None = None

    def read_if_changed(self) -> Snapshot | None:
        """A new Snapshot, or None when the file is unchanged since the last successful read."""

        try:
            info = self.path.stat()
        except FileNotFoundError:
            raise AuthStoreError(
                f"{self.path} not found; is Home Assistant's configuration mapped into the app?"
            ) from None
        except OSError as err:
            raise AuthStoreError(f"Cannot access {self.path}: {err.strerror}") from None
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp == self._stamp:
            return None
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as err:
            raise AuthStoreError(f"Cannot read {self.path}: {err.strerror}") from None
        snapshot = parse(text)
        del text
        self._stamp = stamp
        return snapshot

    def force(self) -> None:
        """Make the next read_if_changed() re-read the file."""

        self._stamp = None
