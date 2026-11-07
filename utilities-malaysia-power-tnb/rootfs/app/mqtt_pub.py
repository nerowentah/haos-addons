"""MQTT discovery: one Home Assistant device per TNB account."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt.client as mqtt
from config import Account

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "power_tnb"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/status"
HA_STATUS_TOPIC = f"{DISCOVERY_PREFIX}/status"
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


def sensor_config(account: Account, sensor: SensorDef, version: str) -> dict[str, object]:
    """Discovery payload for one account sensor."""

    unique_id = f"{BASE_TOPIC}_{account.account_id}_{sensor.key}"
    payload: dict[str, object] = {
        "name": sensor.name,
        "unique_id": unique_id,
        "default_entity_id": f"sensor.tnb_{account.account_id}_{sensor.key}",
        "state_topic": state_topic(account.account_id),
        "value_template": f"{{{{ value_json.{sensor.key} }}}}",
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
        "origin": {"name": "Power TNB", "sw_version": version},
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
        "name": "TNB off-peak holiday today",
        "unique_id": f"{BASE_TOPIC}_holiday_today",
        "default_entity_id": "binary_sensor.tnb_holiday_today",
        "state_topic": f"{BASE_TOPIC}/holiday_today",
        "availability_topic": AVAILABILITY_TOPIC,
        "icon": "mdi:calendar-star",
        "origin": {"name": "Power TNB", "sw_version": version},
    }


class Publisher:
    """Wraps the paho client: LWT availability, retained discovery and states."""

    def __init__(self, broker: dict[str, object], on_ha_online: Callable[[], None]) -> None:
        self._on_ha_online = on_ha_online
        self._connected = threading.Event()
        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="power_tnb")
        except AttributeError:  # paho-mqtt 1.x
            self._client = mqtt.Client(client_id="power_tnb")
        if broker.get("username"):
            self._client.username_pw_set(str(broker["username"]), str(broker.get("password") or ""))
        if broker.get("ssl"):
            self._client.tls_set()
        self._client.will_set(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._host = str(broker["host"])
        self._port = int(broker.get("port") or 1883)

    def start(self, timeout: float = 30.0) -> None:
        """Connect and start the network thread."""

        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            _LOGGER.warning("MQTT broker not reachable yet; will keep retrying in the background")

    def stop(self) -> None:
        """Publish offline and disconnect."""

        try:
            self._client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True).wait_for_publish(5)
        except (RuntimeError, ValueError):
            _LOGGER.debug("Could not publish offline status", exc_info=True)
        self._client.disconnect()
        self._client.loop_stop()

    def _handle_connect(
        self, client: mqtt.Client, _userdata: object, _flags: object, reason: object, *_: object
    ) -> None:
        if getattr(reason, "is_failure", False) or (isinstance(reason, int) and reason != 0):
            _LOGGER.error("MQTT connection refused: %s", reason)
            return
        _LOGGER.info("Connected to the MQTT broker")
        client.subscribe(HA_STATUS_TOPIC)
        client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
        self._connected.set()
        self._on_ha_online()

    def _handle_disconnect(self, *_args: object) -> None:
        if self._connected.is_set():
            _LOGGER.warning("Disconnected from the MQTT broker; reconnecting")
        self._connected.clear()

    def _handle_message(self, _client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
        if message.topic == HA_STATUS_TOPIC and message.payload == b"online":
            _LOGGER.info("Home Assistant came online; republishing discovery")
            self._on_ha_online()

    def publish_json(self, topic: str, payload: object, *, retain: bool = True) -> None:
        """Publish a JSON payload (``None`` publishes an empty retained message to clear the topic)."""

        body = "" if payload is None else json.dumps(payload, separators=(",", ":"))
        self._client.publish(topic, body, qos=1, retain=retain)

    def publish_raw(self, topic: str, payload: str, *, retain: bool = True) -> None:
        """Publish a plain string."""

        self._client.publish(topic, payload, qos=1, retain=retain)
