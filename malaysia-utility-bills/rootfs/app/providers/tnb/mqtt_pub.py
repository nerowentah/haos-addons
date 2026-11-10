"""MQTT discovery: one Home Assistant device per TNB account."""

from __future__ import annotations

from dataclasses import dataclass

from core import mqtt as bridge

from .config import Account

DISCOVERY_PREFIX = bridge.DISCOVERY_PREFIX
BASE_TOPIC = "power_tnb"
AVAILABILITY_TOPIC = bridge.AVAILABILITY_TOPIC  # the app-wide bridge status, not per provider
SOURCE_PORTAL = "portal"
SOURCE_METER = "meter"


@dataclass(frozen=True, slots=True)
class SensorDef:
    """A sensor published for every account that has ``source`` configured."""

    key: str
    name: str
    source: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    diagnostic: bool = False
    attributes: bool = False
    last_reset_key: str | None = None
    precision: int | None = None
    component: str = "sensor"


SENSORS: tuple[SensorDef, ...] = (
    # myTNB portal (billing-grade, about two days behind)
    SensorDef(
        "portal_usage_cycle",
        "Portal usage this cycle",
        SOURCE_PORTAL,
        "kWh",
        "energy",
        "total",
        precision=2,
        last_reset_key="period_start",
    ),
    SensorDef(
        "portal_cost_cycle",
        "Portal cost this cycle",
        SOURCE_PORTAL,
        "MYR",
        "monetary",
        "total",
        precision=2,
        last_reset_key="period_start",
    ),
    SensorDef(
        "portal_usage_latest_day",
        "Portal usage latest day",
        SOURCE_PORTAL,
        "kWh",
        "energy",
        precision=2,
        attributes=True,
    ),
    SensorDef("portal_cost_latest_day", "Portal cost latest day", SOURCE_PORTAL, "MYR", "monetary", precision=2),
    SensorDef("amount_due", "Amount due", SOURCE_PORTAL, "MYR", "monetary", "total", precision=2),
    SensorDef("payment_due", "Payment due", SOURCE_PORTAL, icon="mdi:cash-clock", component="binary_sensor"),
    SensorDef("due_date", "Due date", SOURCE_PORTAL, device_class="date", icon="mdi:calendar-alert"),
    SensorDef("portal_data_as_of", "Portal data as of", SOURCE_PORTAL, device_class="timestamp", diagnostic=True),
    SensorDef("portal_status", "Portal status", SOURCE_PORTAL, icon="mdi:cloud-check-outline", diagnostic=True),
    SensorDef("portal_last_sync", "Portal last sync", SOURCE_PORTAL, device_class="timestamp", diagnostic=True),
    # Live estimate from the local energy sensor
    SensorDef(
        "bill_estimate",
        "Bill estimate",
        SOURCE_METER,
        "MYR",
        "monetary",
        "total",
        attributes=True,
        last_reset_key="period_start",
        precision=2,
    ),
    SensorDef(
        "predicted_bill", "Predicted bill", SOURCE_METER, "MYR", "monetary", icon="mdi:crystal-ball", precision=2
    ),
    SensorDef("predicted_kwh", "Predicted usage", SOURCE_METER, "kWh", "energy", precision=1),
    SensorDef("cycle_import", "Usage this cycle", SOURCE_METER, "kWh", "energy", "total_increasing", precision=2),
    SensorDef("cycle_peak", "Peak usage this cycle", SOURCE_METER, "kWh", "energy", "total_increasing", precision=2),
    SensorDef(
        "cycle_offpeak", "Off-peak usage this cycle", SOURCE_METER, "kWh", "energy", "total_increasing", precision=2
    ),
    SensorDef("cycle_export", "Export this cycle", SOURCE_METER, "kWh", "energy", "total_increasing", precision=2),
    SensorDef("today_import", "Usage today", SOURCE_METER, "kWh", "energy", "total_increasing", precision=2),
    SensorDef("today_export", "Export today", SOURCE_METER, "kWh", "energy", "total_increasing", precision=2),
    SensorDef("current_rate", "Current rate", SOURCE_METER, "MYR/kWh", icon="mdi:cash-clock", precision=4),
    SensorDef("current_period", "Current period", SOURCE_METER, icon="mdi:clock-time-four-outline"),
    SensorDef("tier", "Tariff tier", SOURCE_METER, icon="mdi:stairs"),
    SensorDef("days_remaining", "Days left in cycle", SOURCE_METER, "d", icon="mdi:calendar-end", diagnostic=True),
    SensorDef(
        "nem_carry_kwh", "NEM credit carried forward", SOURCE_METER, "kWh", "energy", diagnostic=True, precision=2
    ),
)


def sensors_for(account: Account) -> list[SensorDef]:
    """Sensors that apply to an account's configured sources."""

    sources = {SOURCE_PORTAL} if account.has_portal else set()
    if account.has_meter:
        sources.add(SOURCE_METER)
    return [s for s in SENSORS if s.source in sources]


def entity_id(account_id: str, key: str, component: str = "sensor") -> str:
    """Entity id of an account sensor (the same for MQTT and the states-API fallback)."""

    return f"{component}.tnb_{account_id}_{key}"


HOLIDAY_ENTITY_ID = "binary_sensor.tnb_holiday_today"
HOLIDAY_NAME = "TNB off-peak holiday today"


def state_attributes(account: Account, sensor: SensorDef) -> dict[str, object]:
    """Static attributes for a sensor posted through the Home Assistant states API."""

    attributes: dict[str, object] = {
        "friendly_name": f"TNB {account.name} {sensor.name}",
        "unit_of_measurement": sensor.unit,
        "device_class": sensor.device_class,
        "state_class": sensor.state_class,
        "icon": sensor.icon,
    }
    return {k: v for k, v in attributes.items() if v is not None}


def state_value(sensor: SensorDef, value: object) -> str:
    """State string for the states API (rounded like the MQTT display precision)."""

    if value is None:
        return "unknown"
    if sensor.component == "binary_sensor":
        return "on" if value else "off"
    if isinstance(value, float) and sensor.precision is not None:
        return f"{value:.{sensor.precision}f}"
    return str(value)


def state_topic(account_id: str) -> str:
    """JSON state topic of an account."""

    return f"{BASE_TOPIC}/{account_id}/state"


def source_availability_topic(account_id: str, source: str) -> str:
    """Availability of one account source (offline while switched off in the panel)."""

    return f"{BASE_TOPIC}/{account_id}/{source}/availability"


def attributes_topic(account_id: str, key: str) -> str:
    """JSON attributes topic of one sensor."""

    return f"{BASE_TOPIC}/{account_id}/{key}/attributes"


def discovery_topic(component: str, object_id: str) -> str:
    """Discovery config topic."""

    return f"{DISCOVERY_PREFIX}/{component}/{BASE_TOPIC}/{object_id}/config"


def value_template(sensor: SensorDef) -> str:
    """Template reading one key of the account's JSON state (ON/OFF/None for a binary sensor)."""

    if sensor.component == "binary_sensor":
        key = f"value_json.{sensor.key}"
        return f"{{% if {key} is true %}}ON{{% elif {key} is false %}}OFF{{% else %}}None{{% endif %}}"
    return f"{{{{ value_json.{sensor.key} }}}}"


def sensor_config(account: Account, sensor: SensorDef, version: str) -> dict[str, object]:
    """Discovery payload for one account sensor."""

    unique_id = f"{BASE_TOPIC}_{account.account_id}_{sensor.key}"
    payload: dict[str, object] = {
        "name": sensor.name,
        "unique_id": unique_id,
        "default_entity_id": entity_id(account.account_id, sensor.key, sensor.component),
        "state_topic": state_topic(account.account_id),
        "value_template": value_template(sensor),
        "availability": [
            {"topic": AVAILABILITY_TOPIC},
            {"topic": source_availability_topic(account.account_id, sensor.source)},
        ],
        "availability_mode": "all",
        "device": {
            "identifiers": [f"{BASE_TOPIC}_{account.account_id}"],
            "name": f"TNB {account.name}",
            "manufacturer": "Tenaga Nasional Berhad",
            "model": f"Domestic ({'Time-of-Use' if account.tariff == 'tou' else 'General'})",
            "sw_version": version,
        },
        "origin": {"name": "Malaysia Utility Bills", "sw_version": version},
    }
    optional = {
        "unit_of_measurement": sensor.unit,
        "device_class": sensor.device_class,
        "state_class": sensor.state_class,
        "icon": sensor.icon,
        "suggested_display_precision": sensor.precision,
        "entity_category": "diagnostic" if sensor.diagnostic else None,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    if sensor.attributes:
        payload["json_attributes_topic"] = attributes_topic(account.account_id, sensor.key)
    if sensor.last_reset_key:
        payload["last_reset_value_template"] = f"{{{{ value_json.{sensor.last_reset_key} }}}}"
    return payload


def holiday_config(version: str) -> dict[str, object]:
    """Discovery payload for the shared ToU holiday binary sensor."""

    return {
        "name": HOLIDAY_NAME,
        "unique_id": f"{BASE_TOPIC}_holiday_today",
        "default_entity_id": HOLIDAY_ENTITY_ID,
        "state_topic": f"{BASE_TOPIC}/holiday_today",
        "availability_topic": AVAILABILITY_TOPIC,
        "icon": "mdi:calendar-star",
        "origin": {"name": "Malaysia Utility Bills", "sw_version": version},
    }
