"""MQTT discovery: one Home Assistant device per Ranhill SAJ account."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from config import Account

if TYPE_CHECKING:
    import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "ranhill_saj"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/status"
HA_STATUS_TOPIC = f"{DISCOVERY_PREFIX}/status"
APP_NAME = "Ranhill SAJ Bill Checker"


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


class Publisher:
    """Wraps the paho client: LWT availability, retained discovery and states."""

    def __init__(self, broker: dict[str, object], on_ha_online: Callable[[], None]) -> None:
        # paho (and ssl) are only loaded once a broker exists, keeping MQTT-less installs lighter.
        import paho.mqtt.client as mqtt  # noqa: PLC0415 - lazy import

        self._on_ha_online = on_ha_online
        self._sent: dict[str, str] = {}  # last retained payload per topic, to skip unchanged publishes
        self._connected = threading.Event()
        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=BASE_TOPIC)
        except AttributeError:  # paho-mqtt 1.x
            self._client = mqtt.Client(client_id=BASE_TOPIC)
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

    @property
    def connected(self) -> bool:
        """Whether the broker connection is up."""

        return self._connected.is_set()

    def start(self, timeout: float = 5.0) -> None:
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
        self.forget_sent()
        self._on_ha_online()

    def _handle_disconnect(self, *_args: object) -> None:
        if self._connected.is_set():
            _LOGGER.warning("Disconnected from the MQTT broker; reconnecting")
        self._connected.clear()

    def _handle_message(self, _client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
        if message.topic == HA_STATUS_TOPIC and message.payload == b"online":
            _LOGGER.info("Home Assistant came online; republishing discovery")
            self.forget_sent()
            self._on_ha_online()

    def publish_json(self, topic: str, payload: object, *, retain: bool = True) -> None:
        """Publish a JSON payload (``None`` publishes an empty retained message to clear the topic)."""

        body = "" if payload is None else json.dumps(payload, separators=(",", ":"))
        self.publish_raw(topic, body, retain=retain)

    def publish_raw(self, topic: str, payload: str, *, retain: bool = True) -> None:
        """Publish a plain string; an unchanged retained payload is skipped."""

        if retain and self._sent.get(topic) == payload:
            return
        self._client.publish(topic, payload, qos=1, retain=retain)
        if retain:
            self._sent[topic] = payload

    def forget_sent(self) -> None:
        """Republish everything next time (after a reconnect or a Home Assistant restart)."""

        self._sent.clear()
