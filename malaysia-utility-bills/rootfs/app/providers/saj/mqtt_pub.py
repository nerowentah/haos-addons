"""MQTT discovery: one Home Assistant device per Ranhill SAJ account."""

from __future__ import annotations

from dataclasses import dataclass

from core import mqtt as bridge

from .config import Account

DISCOVERY_PREFIX = bridge.DISCOVERY_PREFIX
BASE_TOPIC = "ranhill_saj"
AVAILABILITY_TOPIC = bridge.AVAILABILITY_TOPIC  # the app-wide bridge status, not per provider
APP_NAME = "Malaysia Utility Bills"


@dataclass(frozen=True, slots=True)
class SensorDef:
    """A sensor published for every account."""

    key: str
    name: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    diagnostic: bool = False
    attributes: bool = False
    precision: int | None = None
    component: str = "sensor"


SENSORS: tuple[SensorDef, ...] = (
    SensorDef("amount_due", "Amount due", "MYR", "monetary", "total", attributes=True, precision=2),
    SensorDef("payment_due", "Payment due", icon="mdi:cash-clock", component="binary_sensor"),
    SensorDef("amount_changed", "Amount due changed", device_class="timestamp", icon="mdi:cash-sync"),
    SensorDef("status", "Check status", icon="mdi:water-check-outline", diagnostic=True),
    SensorDef("last_checked", "Last checked", device_class="timestamp", diagnostic=True),
)


def entity_id(account_id: str, key: str, component: str = "sensor") -> str:
    """Entity id of an account sensor (the same for MQTT and the states-API fallback)."""

    return f"{component}.saj_{account_id}_{key}"


def state_attributes(account: Account, sensor: SensorDef) -> dict[str, object]:
    """Static attributes for a sensor posted through the Home Assistant states API."""

    attributes: dict[str, object] = {
        "friendly_name": f"SAJ {account.name} {sensor.name}",
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


def account_availability_topic(account_id: str) -> str:
    """Availability of one account (offline while switched off in the panel)."""

    return f"{BASE_TOPIC}/{account_id}/availability"


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

    payload: dict[str, object] = {
        "name": sensor.name,
        "unique_id": f"{BASE_TOPIC}_{account.account_id}_{sensor.key}",
        "default_entity_id": entity_id(account.account_id, sensor.key, sensor.component),
        "state_topic": state_topic(account.account_id),
        "value_template": value_template(sensor),
        "availability": [
            {"topic": AVAILABILITY_TOPIC},
            {"topic": account_availability_topic(account.account_id)},
        ],
        "availability_mode": "all",
        "device": {
            "identifiers": [f"{BASE_TOPIC}_{account.account_id}"],
            "name": f"SAJ {account.name}",
            "manufacturer": "Ranhill SAJ",
            "model": "Water account",
            "sw_version": version,
        },
        "origin": {"name": APP_NAME, "sw_version": version},
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
    return payload
