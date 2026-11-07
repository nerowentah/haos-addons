"""Parse and validate the add-on options (/data/options.json)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import tariff

CONFIG_PATH = Path("/data/options.json")
SMARTMETER_PATH_RE = re.compile(r"/AccountManagement/SmartMeter/Index/TRIL\?caNo=(\d+)")
ACCOUNT_NUMBER_RE = re.compile(r"^\d{10,14}$")
SMARTMETER_PATH = "/AccountManagement/SmartMeter/Index/TRIL?caNo={ca_no}"
ENTITY_ID_RE = re.compile(r"^sensor\.[a-z0-9_]+$")
TARIFFS = {tariff.TARIFF_GENERAL, tariff.TARIFF_TOU}


class ConfigError(ValueError):
    """Raised when the add-on configuration is invalid."""


@dataclass(slots=True)
class Account:
    """One TNB account: a myTNB portal login and/or a local energy sensor."""

    name: str
    account_id: str
    tariff: str
    billing_start_day: int
    mytnb_username: str = ""
    mytnb_password: str = ""
    ca_no: str = ""
    import_entity: str = ""
    export_entity: str = ""
    enabled: bool = True
    portal_enabled: bool = True
    meter_enabled: bool = True
    statistics_enabled: bool = True

    @property
    def portal_active(self) -> bool:
        """Whether the portal should be synced right now (configured and switched on)."""

        return self.has_portal and self.enabled and self.portal_enabled

    @property
    def meter_active(self) -> bool:
        """Whether the energy sensor should be polled right now (configured and switched on)."""

        return self.has_meter and self.enabled and self.meter_enabled

    @property
    def has_portal(self) -> bool:
        """Whether myTNB portal credentials are configured."""

        return bool(self.mytnb_username and self.mytnb_password and self.ca_no)

    @property
    def smartmeter_path(self) -> str:
        """The myTNB smart meter page of this account."""

        return SMARTMETER_PATH.format(ca_no=self.ca_no)

    @property
    def has_meter(self) -> bool:
        """Whether a local energy sensor is configured."""

        return bool(self.import_entity)


@dataclass(slots=True)
class Config:
    """Parsed add-on options."""

    portal_interval_hours: int
    meter_poll_seconds: int
    afa_rate: float
    calendarific_api_key: str
    extra_holidays: list[date]
    notify_on_failure: bool
    accounts: list[Account]
    mqtt: bool = True


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


def _int(raw: dict[str, object], key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, low), high)


def _account_number(name: str, entry: dict[str, object]) -> str:
    """The TNB account number (caNo), from ``account_number`` or the legacy ``smartmeter_url``."""

    number = re.sub(r"[\s-]", "", _str(entry, "account_number"))
    if number:
        if not ACCOUNT_NUMBER_RE.match(number):
            raise ConfigError(
                f"Account '{name}': account_number must be the 10-14 digit number printed on your TNB bill"
            )
        return number
    url = _str(entry, "smartmeter_url")
    if url:
        match = SMARTMETER_PATH_RE.search(url)
        if not match:
            raise ConfigError(
                f"Account '{name}': smartmeter_url must contain /AccountManagement/SmartMeter/Index/TRIL?caNo=...; "
                "easier: remove it and set account_number to the number on your bill"
            )
        return match.group(1)
    return ""


def build_account(index: int, entry: object, default_login: tuple[str, str] = ("", "")) -> Account:
    """Validate one ``accounts`` row (``default_login`` is the shared top-level myTNB login)."""

    if not isinstance(entry, dict):
        raise ConfigError(f"Account #{index} must be a mapping of options")
    name = _str(entry, "name") or f"Account {index}"
    account_id = slugify(name)
    if not account_id:
        raise ConfigError(f"Account #{index}: name '{name}' must contain letters or digits")

    tariff_name = _str(entry, "tariff").lower() or tariff.TARIFF_GENERAL
    if tariff_name not in TARIFFS:
        raise ConfigError(f"Account '{name}': tariff must be one of {', '.join(sorted(TARIFFS))}")

    account = Account(
        name=name,
        account_id=account_id,
        tariff=tariff_name,
        billing_start_day=_int(entry, "billing_start_day", 1, 1, 31),
        mytnb_username=_str(entry, "mytnb_username") or default_login[0],
        mytnb_password=_str(entry, "mytnb_password") or default_login[1],
        ca_no=_account_number(name, entry),
        import_entity=_str(entry, "import_entity").lower(),
        export_entity=_str(entry, "export_entity").lower(),
        enabled=_bool(entry, "enabled"),
        portal_enabled=_bool(entry, "portal_enabled"),
        meter_enabled=_bool(entry, "meter_enabled"),
        statistics_enabled=_bool(entry, "statistics_enabled"),
    )

    own_login = bool(_str(entry, "mytnb_username") or _str(entry, "mytnb_password"))
    has_login = bool(account.mytnb_username and account.mytnb_password)
    if own_login and not has_login:
        raise ConfigError(f"Account '{name}': set both mytnb_username and mytnb_password (or neither)")
    if account.ca_no and not has_login:
        raise ConfigError(
            f"Account '{name}': set mytnb_username and mytnb_password, either at the top "
            "(shared by all accounts) or on this account"
        )
    if own_login and not account.ca_no:
        raise ConfigError(
            f"Account '{name}': add account_number, the 12-digit number printed on your TNB bill "
            "(also shown in the myTNB app under the account)"
        )
    for key in ("import_entity", "export_entity"):
        value = getattr(account, key)
        if value and not ENTITY_ID_RE.match(value):
            raise ConfigError(f"Account '{name}': {key} must be a sensor entity id such as sensor.grid_energy")
    if account.export_entity and not account.import_entity:
        raise ConfigError(f"Account '{name}': export_entity requires import_entity")
    if not (account.has_portal or account.has_meter):
        raise ConfigError(f"Account '{name}' needs an account_number (with a myTNB login), an import_entity, or both")
    return account


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config."""

    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError(
            "No accounts configured; add at least one account with a name, tariff, billing_start_day "
            "and an account_number and/or import_entity"
        )
    username, password = _str(raw, "mytnb_username"), _str(raw, "mytnb_password")
    if bool(username) != bool(password):
        raise ConfigError("Set both mytnb_username and mytnb_password at the top (or neither)")
    if username and "@" not in username:
        raise ConfigError("mytnb_username must be the email address you use to log in to myTNB")
    accounts = [build_account(index, entry, (username, password)) for index, entry in enumerate(accounts_raw, start=1)]
    seen: set[str] = set()
    for account in accounts:
        if account.account_id in seen:
            raise ConfigError(f"Two accounts share the id '{account.account_id}'; give them distinct names")
        seen.add(account.account_id)

    afa_raw = raw.get("afa_rate", 0.0)
    afa_rate = float(afa_raw) if isinstance(afa_raw, int | float) and not isinstance(afa_raw, bool) else 0.0

    extra_holidays: list[date] = []
    for value in raw.get("extra_holidays") or []:  # type: ignore[union-attr]
        try:
            extra_holidays.append(date.fromisoformat(str(value).strip()))
        except ValueError as err:
            raise ConfigError(f"extra_holidays: '{value}' is not a YYYY-MM-DD date") from err

    return Config(
        portal_interval_hours=_int(raw, "portal_interval_hours", 6, 1, 24),
        meter_poll_seconds=_int(raw, "meter_poll_seconds", 60, 10, 600),
        afa_rate=afa_rate,
        calendarific_api_key=_str(raw, "calendarific_api_key"),
        extra_holidays=extra_holidays,
        notify_on_failure=bool(raw.get("notify_on_failure", True)),
        accounts=accounts,
        mqtt=_bool(raw, "mqtt"),
    )


def mask_number(number: str) -> str:
    """Show only the first and last 4 digits of an account number."""

    return number if len(number) <= 8 else f"{number[:4]}••••{number[-4:]}"  # noqa: PLR2004 - 4+4 digits


def mask_email(email: str) -> str:
    """Show only the first letter of the mailbox, e.g. y••@example.com."""

    user, _, domain = email.partition("@")
    return f"{user[:1]}••@{domain}" if domain else "••"
