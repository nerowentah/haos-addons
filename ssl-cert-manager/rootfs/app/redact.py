"""Keep secrets out of logs and reprs (shared by the apps in this repo).

* :class:`Secret` wraps a token or password: printing or logging it shows
  ``***``, and the value is only available through :meth:`Secret.reveal`.
* :func:`register` and :func:`install` are the safety net: every registered
  value is replaced by ``***`` in each log line, tracebacks included.

Edit this file in ``shared/app`` only, then run ``python tools/sync_shared.py``.
"""

from __future__ import annotations

import hmac
import logging
import threading

MASK = "***"
MIN_LENGTH = 8  # shorter values would blank out ordinary words

_lock = threading.Lock()
_values: tuple[str, ...] = ()


def register(value: str) -> None:
    """Never let ``value`` appear in a log line (values under 8 characters are ignored)."""

    global _values  # noqa: PLW0603 - one registry per process
    text = value.strip()
    if len(text) < MIN_LENGTH:
        return
    with _lock:
        if text not in _values:
            # Longest first, so a value that contains another is masked whole.
            _values = tuple(sorted({*_values, text}, key=len, reverse=True))


def scrub(text: str) -> str:
    """``text`` with every registered value replaced by ``***``."""

    for value in _values:
        if value in text:
            text = text.replace(value, MASK)
    return text


class Secret:
    """A token or password that does not leak through ``str()``, ``repr()`` or logging."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value
        register(value)

    def reveal(self) -> str:
        """The value itself: call this only where it is sent or written."""

        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode("utf-8"), other._value.encode("utf-8"))

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"Secret({MASK})" if self._value else "Secret()"

    def __str__(self) -> str:
        return MASK if self._value else ""


class RedactingFormatter(logging.Formatter):
    """Formats like the wrapped formatter, then masks the registered values."""

    def __init__(self, inner: logging.Formatter | None = None) -> None:
        super().__init__()
        self._inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return scrub(self._inner.format(record))


def install(logger: logging.Logger | None = None) -> None:
    """Mask registered values in everything the handlers of ``logger`` (default: root) write."""

    for handler in (logger or logging.getLogger()).handlers:
        if not isinstance(handler.formatter, RedactingFormatter):
            handler.setFormatter(RedactingFormatter(handler.formatter))
