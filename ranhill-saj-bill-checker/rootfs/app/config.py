"""Parse and validate the app options (/data/options.json)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path("/data/options.json")
# 8 digits, 1 letter, 7 digits, e.g. 91234582-J2542356 (Ranhill SAJ's own example on the bill page).
ACCOUNT_NUMBER_RE = re.compile(r"^\d{8}[A-Z]\d{7}$")
ACCOUNT_EXAMPLE = "12345678-A1234567"

TRACE = 5
NOTICE = 25
LOG_LEVELS = {
    "trace": TRACE,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": NOTICE,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def parse_log_level(raw: dict[str, object]) -> str:
    """The ``log_level`` option, lower-cased ("info" when missing or unknown)."""

    value = str(raw.get("log_level") or "info").strip().lower()
    return value if value in LOG_LEVELS else "info"


def apply_log_level(name: str) -> None:
    """Set the root logger to ``name`` (one of LOG_LEVELS)."""

    logging.addLevelName(TRACE, "TRACE")
    logging.addLevelName(NOTICE, "NOTICE")
    logging.getLogger().setLevel(LOG_LEVELS.get(name, logging.INFO))


class ConfigError(ValueError):
    """Raised when the app configuration is invalid."""


@dataclass(slots=True)
class Account:
    """One Ranhill SAJ water account."""

    name: str
    account_id: str
    account_number: str
    enabled: bool = True


@dataclass(slots=True)
class Config:
    """Parsed app options."""

    notify_on_failure: bool
    accounts: list[Account]
    mqtt: bool = True
    notify_on_unpaid: bool = True
    log_level: str = "info"


def slugify(value: str) -> str:
    """Lower-case ``value`` and collapse anything but [a-z0-9] into underscores."""

    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def load_options(path: Path = CONFIG_PATH) -> dict[str, object]:
    """Read and parse the supervisor options file."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"Configuration file not found at {path}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"Invalid JSON in {path}: {err}") from err
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration root in {path} must be a mapping")
    return raw


def _str(entry: dict[str, object], key: str) -> str:
    return str(entry.get(key) or "").strip()


def _bool(entry: dict[str, object], key: str, default: bool = True) -> bool:
    value = entry.get(key)
    return value if isinstance(value, bool) else default


def normalize_account_number(value: str) -> str:
    """Upper-case the number and drop spaces and hyphens ("" when it is not a valid SAJ account number)."""

    number = re.sub(r"[\s-]", "", value).upper()
    return number if ACCOUNT_NUMBER_RE.match(number) else ""


def build_account(index: int, entry: object) -> Account:
    """Validate one ``accounts`` row."""

    if not isinstance(entry, dict):
        raise ConfigError(f"Account #{index} must be a mapping of options")
    name = _str(entry, "name") or f"Account {index}"
    account_id = slugify(name)
    if not account_id:
        raise ConfigError(f"Account #{index}: name '{name}' must contain letters or digits")
    raw_number = _str(entry, "account_number")
    if not raw_number:
        raise ConfigError(
            f"Account '{name}': add account_number, the 16-character number on your Ranhill SAJ bill "
            f"(8 digits, a letter, 7 digits, e.g. {ACCOUNT_EXAMPLE})"
        )
    number = normalize_account_number(raw_number)
    if not number:
        raise ConfigError(
            f"Account '{name}': account_number must be 8 digits, a letter and 7 digits, e.g. {ACCOUNT_EXAMPLE}"
        )
    return Account(name=name, account_id=account_id, account_number=number, enabled=_bool(entry, "enabled"))


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config."""

    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("No accounts configured; add at least one account with a name and an account_number")
    accounts = [build_account(index, entry) for index, entry in enumerate(accounts_raw, start=1)]
    seen_ids: set[str] = set()
    seen_numbers: set[str] = set()
    for account in accounts:
        if account.account_id in seen_ids:
            raise ConfigError(f"Two accounts share the id '{account.account_id}'; give them distinct names")
        if account.account_number in seen_numbers:
            raise ConfigError(f"Account '{account.name}' repeats an account number that is already listed")
        seen_ids.add(account.account_id)
        seen_numbers.add(account.account_number)
    return Config(
        notify_on_failure=_bool(raw, "notify_on_failure"),
        accounts=accounts,
        mqtt=_bool(raw, "mqtt"),
        notify_on_unpaid=_bool(raw, "notify_on_unpaid"),
        log_level=parse_log_level(raw),
    )


def mask_number(number: str) -> str:
    """Show only the first and last 4 characters of an account number."""

    return number if len(number) <= 8 else f"{number[:4]}••••{number[-4:]}"  # noqa: PLR2004 - 4+4 characters


def mask_name(name: str) -> str:
    """Keep the first letter of each word of the account holder's name, e.g. "A•••• B••••"."""

    return " ".join(f"{word[0]}••••" for word in name.split()) or "-"
