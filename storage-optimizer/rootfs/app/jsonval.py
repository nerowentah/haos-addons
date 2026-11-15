"""Typed access to parsed JSON (shared by the apps in this repo).

Options, panel requests, saved state and API replies all arrive as JSON whose
shape nobody guarantees. These helpers check the shape where the value enters
the app and raise :class:`ShapeError` (a ``ValueError``) when it is wrong, so
the rest of the code works with real types instead of ``object``.

Edit this file in ``shared/app`` only, then run ``python tools/sync_shared.py``.
"""

from __future__ import annotations

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]


class ShapeError(ValueError):
    """A JSON value does not have the expected type."""


def as_dict(value: object, what: str = "value") -> JsonObject:
    """``value`` as a JSON object."""

    if not isinstance(value, dict):
        raise ShapeError(f"{what} must be an object")
    return value


def as_list(value: object, what: str = "value") -> list[JsonValue]:
    """``value`` as a JSON array."""

    if not isinstance(value, list):
        raise ShapeError(f"{what} must be a list")
    return value


def get_dict(doc: JsonObject, key: str) -> JsonObject:
    """``doc[key]`` as an object; a missing or null key is an empty object."""

    value = doc.get(key)
    return {} if value is None else as_dict(value, key)


def get_list(doc: JsonObject, key: str) -> list[JsonValue]:
    """``doc[key]`` as an array; a missing or null key is an empty array."""

    value = doc.get(key)
    return [] if value is None else as_list(value, key)


def get_str(doc: JsonObject, key: str, default: str = "") -> str:
    """``doc[key]`` as a string; ``default`` when the key is missing or null."""

    value = doc.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ShapeError(f"{key} must be text")
    return value


def get_bool(doc: JsonObject, key: str, default: bool = False) -> bool:
    """``doc[key]`` as a boolean; ``default`` when the key is missing or null."""

    value = doc.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ShapeError(f"{key} must be true or false")
    return value


def get_int(doc: JsonObject, key: str, default: int = 0) -> int:
    """``doc[key]`` as a whole number; ``default`` when the key is missing or null."""

    value = doc.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ShapeError(f"{key} must be a whole number")
    return value


def get_float(doc: JsonObject, key: str, default: float = 0.0) -> float:
    """``doc[key]`` as a number; ``default`` when the key is missing or null."""

    value = doc.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShapeError(f"{key} must be a number")
    return float(value)


def str_list(value: object, what: str = "value") -> list[str]:
    """``value`` as a list of strings."""

    items = as_list(value, what)
    if not all(isinstance(item, str) for item in items):
        raise ShapeError(f"{what} must be a list of text")
    return [str(item) for item in items]
