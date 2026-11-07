"""Parse and validate the add-on options (/data/options.json)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import tariff

CONFIG_PATH = Path("/data/options.json")
SMARTMETER_PATH_RE = re.compile(r"/AccountManagement/SmartMeter/Index/TRIL\?caNo=[^\"\s]+")
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
    smartmeter_url: str = ""
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

        return bool(self.mytnb_username and self.mytnb_password and self.smartmeter_url)

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


def build_account(index: int, entry: object) -> Account:
    """Validate one ``accounts`` row."""

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
        mytnb_username=_str(entry, "mytnb_username"),
        mytnb_password=_str(entry, "mytnb_password"),
        smartmeter_url=_str(entry, "smartmeter_url"),
        import_entity=_str(entry, "import_entity").lower(),
        export_entity=_str(entry, "export_entity").lower(),
        enabled=_bool(entry, "enabled"),
        portal_enabled=_bool(entry, "portal_enabled"),
        meter_enabled=_bool(entry, "meter_enabled"),
        statistics_enabled=_bool(entry, "statistics_enabled"),
    )

    portal_fields = [account.mytnb_username, account.mytnb_password, account.smartmeter_url]
    if any(portal_fields) and not all(portal_fields):
        raise ConfigError(f"Account '{name}': myTNB needs all of mytnb_username, mytnb_password and smartmeter_url")
    if account.smartmeter_url and not SMARTMETER_PATH_RE.search(account.smartmeter_url):
        raise ConfigError(
            f"Account '{name}': smartmeter_url must be the myTNB 'View Energy Usage' link "
            "containing /AccountManagement/SmartMeter/Index/TRIL?caNo=..."
        )
    for key in ("import_entity", "export_entity"):
        value = getattr(account, key)
        if value and not ENTITY_ID_RE.match(value):
            raise ConfigError(f"Account '{name}': {key} must be a sensor entity id such as sensor.grid_energy")
    if account.export_entity and not account.import_entity:
        raise ConfigError(f"Account '{name}': export_entity requires import_entity")
    if not (account.has_portal or account.has_meter):
        raise ConfigError(f"Account '{name}' needs myTNB credentials, an import_entity, or both")
    return account


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config."""

    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ConfigError("No accounts configured; add at least one account")
    accounts = [build_account(index, entry) for index, entry in enumerate(accounts_raw, start=1)]
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
