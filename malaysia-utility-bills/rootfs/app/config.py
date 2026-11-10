"""The app options: shared switches plus one ``<provider>_`` group of options per provider."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from core.options import ConfigError, option_bool, parse_log_level
from providers.base import Engine
from providers.saj import config as saj_config
from providers.saj import engine as saj_engine
from providers.saj import panel as saj_panel
from providers.tnb import config as tnb_config
from providers.tnb import engine as tnb_engine
from providers.tnb import panel as tnb_panel

SHARED_KEYS = ("notify_on_failure", "notify_on_unpaid", "mqtt", "log_level")


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """One utility provider: its option group, engine and the app it replaces."""

    key: str  # option prefix without the underscore, e.g. tnb
    label: str
    section_title: str
    icon: str
    old_slug: str  # slug of the app this provider replaces
    old_name: str
    option_keys: tuple[str, ...]  # provider options, without the prefix
    load_config: Callable[[dict[str, object]], object]
    engine: type[Engine]
    apply_toggle: Callable[[dict[str, object], str, bool, str], None]

    @property
    def prefix(self) -> str:
        """Prefix of this provider's options, e.g. ``tnb_``."""

        return f"{self.key}_"

    def options(self, raw: dict[str, object]) -> dict[str, object] | None:
        """This provider's options without the prefix, plus the shared switches; None without accounts."""

        sub = {key.removeprefix(self.prefix): value for key, value in raw.items() if key.startswith(self.prefix)}
        if not sub.get("accounts"):
            return None
        sub.update({key: raw[key] for key in SHARED_KEYS if key in raw})
        return sub


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        key="tnb",
        label="TNB",
        section_title="Electricity · TNB",
        icon="mdi:lightning-bolt",
        old_slug="power_tnb",
        old_name="TNB Usage Checker",
        option_keys=(
            "accounts",
            "portal_interval_hours",
            "meter_poll_seconds",
            "afa_rate",
            "calendarific_api_key",
            "extra_holidays",
            "daily_report",
            "maintenance_windows",
            "debug_login",
            "mytnb_username",
            "mytnb_password",
        ),
        load_config=tnb_config.load_config,
        engine=tnb_engine.Engine,
        apply_toggle=tnb_panel.apply_toggle,
    ),
    ProviderSpec(
        key="saj",
        label="Ranhill SAJ",
        section_title="Water · Ranhill SAJ",
        icon="mdi:water",
        old_slug="ranhill_saj_bill_checker",
        old_name="Ranhill SAJ Bill Checker",
        option_keys=("accounts",),
        load_config=saj_config.load_config,
        engine=saj_engine.Engine,
        apply_toggle=saj_panel.apply_toggle,
    ),
)
PROVIDERS_BY_KEY = {spec.key: spec for spec in PROVIDERS}


@dataclass(slots=True)
class Config:
    """Parsed app options: the shared switches and each configured provider's own config."""

    providers: dict[str, object] = field(default_factory=dict)
    notify_on_failure: bool = True
    notify_on_unpaid: bool = True
    mqtt: bool = True
    log_level: str = "info"

    @property
    def account_count(self) -> int:
        """Accounts across every provider."""

        return sum(len(getattr(cfg, "accounts", [])) for cfg in self.providers.values())


def load_config(raw: dict[str, object]) -> Config:
    """Validate raw options and produce a Config.

    A provider without accounts is left out; with none at all the app still starts, and its panel
    offers to import the accounts of the apps it replaces.
    """

    providers: dict[str, object] = {}
    for spec in PROVIDERS:
        sub = spec.options(raw)
        if sub is None:
            continue
        try:
            providers[spec.key] = spec.load_config(sub)
        except ConfigError as err:
            raise ConfigError(f"{spec.label} ({spec.prefix} options): {err}") from err
    return Config(
        providers=providers,
        notify_on_failure=option_bool(raw, "notify_on_failure"),
        notify_on_unpaid=option_bool(raw, "notify_on_unpaid"),
        mqtt=option_bool(raw, "mqtt"),
        log_level=parse_log_level(raw),
    )


def import_options(spec: ProviderSpec, old: dict[str, object]) -> dict[str, object]:
    """The options of the app ``spec`` replaces, renamed into this app's ``<provider>_`` keys.

    Shared switches are left alone; retired or unknown keys are dropped.
    """

    return {spec.prefix + key: value for key, value in old.items() if key in spec.option_keys}
