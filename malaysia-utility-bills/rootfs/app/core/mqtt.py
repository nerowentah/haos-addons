"""The app's one MQTT connection, shared by every provider (bridge availability and retained publishes)."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
CLIENT_ID = "malaysia_utility_bills"
# One bridge availability topic for the whole app. Each provider keeps its own base topic
# (power_tnb, ranhill_saj) so unique ids, entity ids and state topics match the old apps.
AVAILABILITY_TOPIC = f"{CLIENT_ID}/status"
HA_STATUS_TOPIC = f"{DISCOVERY_PREFIX}/status"


class Publisher:
    """Wraps the paho client: LWT availability, retained discovery and states."""

    def __init__(self, broker: dict[str, object], on_ha_online: Callable[[], None]) -> None:
        # paho (and ssl) are only loaded once a broker exists, keeping MQTT-less installs lighter.
        import paho.mqtt.client as mqtt  # noqa: PLC0415 - lazy import

        self._on_ha_online = on_ha_online
        self._sent: dict[str, str] = {}  # last retained payload per topic, to skip unchanged publishes
        self._connected = threading.Event()
        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID)
        except AttributeError:  # paho-mqtt 1.x
            self._client = mqtt.Client(client_id=CLIENT_ID)
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

    def publish_json(self, topic: str, payload: object, *, retain: bool = True, force: bool = False) -> None:
        """Publish a JSON payload (``None`` publishes an empty retained message to clear the topic)."""

        body = "" if payload is None else json.dumps(payload, separators=(",", ":"))
        self.publish_raw(topic, body, retain=retain, force=force)

    def publish_raw(self, topic: str, payload: str, *, retain: bool = True, force: bool = False) -> None:
        """Publish a plain string; an unchanged retained payload is skipped unless ``force``."""

        if retain and not force and self._sent.get(topic) == payload:
            return
        self._client.publish(topic, payload, qos=1, retain=retain)
        if retain:
            self._sent[topic] = payload

    def forget_sent(self) -> None:
        """Republish everything next time (after a reconnect or a Home Assistant restart)."""

        self._sent.clear()
