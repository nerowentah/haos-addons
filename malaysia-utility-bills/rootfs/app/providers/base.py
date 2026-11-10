"""What the app core offers a provider (``Host``) and what every provider implements (``Engine``)."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from core.mqtt import Publisher
    from core.scoped import ScopedStore
    from core.supervisor import Supervisor


class Host(Protocol):
    """The app core, as seen by a provider."""

    supervisor: Supervisor
    version: str
    publisher: Publisher | None
    stopping: threading.Event
    wake: threading.Event

    def use_states_api(self) -> bool:
        """Whether entities currently go through the Home Assistant states API (no MQTT)."""

    def post_state(self, entity_id: str, state: str, attributes: dict[str, object]) -> bool:
        """Set one entity through the states API (skipped when unchanged); False when it failed."""

    def notify(self, notification_id: str, title: str, message: str, *, bill: bool = False) -> None:
        """Send a failure (or, with ``bill``, an unpaid-bill) notice when that kind is switched on."""

    def notify_targets(self) -> list[str]:
        """The phones that get notifications (an empty list means a persistent notification)."""

    def busy(self) -> bool:
        """Whether to stop slow work now (shutting down, or a panel change waits to be applied)."""


class Engine:
    """One provider's accounts: checks, entities and panel cards.

    Subclasses keep their previous app's base topic and entity ids, so moving to
    this app keeps every entity and its history.
    """

    key: ClassVar[str] = ""
    base_topic: ClassVar[str] = ""  # MQTT base topic, e.g. power_tnb
    entity_prefix: ClassVar[str] = ""  # entity ids start with <domain>.<entity_prefix>_

    def __init__(self, host: Host, config: object, store: ScopedStore) -> None:
        self.host = host
        self.config = config
        self.store = store

    # -- Ownership (so a blocked provider's entities are never removed) ----------
    @classmethod
    def owns_topic(cls, topic: str) -> bool:
        """Whether a discovery topic belongs to this provider."""

        return f"/{cls.base_topic}/" in topic

    @classmethod
    def owns_entity(cls, entity_id: str) -> bool:
        """Whether a states-API entity id belongs to this provider."""

        return entity_id.partition(".")[2].startswith(f"{cls.entity_prefix}_")

    # -- Hooks the core calls ---------------------------------------------------
    def reconcile(self, config: object) -> None:
        """Switch to new provider options, keeping the live state of unchanged accounts."""

        raise NotImplementedError

    def discovery(self) -> list[tuple[str, dict[str, object]]]:
        """Every MQTT discovery (topic, payload) of this provider."""

        raise NotImplementedError

    def entity_ids(self) -> list[str]:
        """Every entity id this provider creates through the states API."""

        raise NotImplementedError

    def publish_all(self) -> None:
        """Publish the state of every account (MQTT or the states API)."""

        raise NotImplementedError

    def tick(self) -> None:
        """Run whatever work is due (stop early when ``host.busy()``)."""

        raise NotImplementedError

    def deadlines(self) -> list[float]:
        """Monotonic times of the next scheduled jobs."""

        raise NotImplementedError

    def account_count(self) -> int:
        """Number of configured accounts."""

        return len(getattr(self.config, "accounts", []))

    def uses_phones(self) -> bool:
        """Whether this provider sends notices beyond the shared failure/unpaid switches."""

        return False

    def log_accounts(self) -> None:
        """Describe the accounts in the log at start-up."""

    def panel_cards(self, config: object) -> list[dict[str, object]]:
        """Account cards for ``config`` (the options to show; ids are namespaced by the core)."""

        raise NotImplementedError

    def panel_settings(self, config: object) -> dict[str, object] | None:  # noqa: ARG002 - optional hook
        """This provider's settings card, if any."""

        return None

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:
        """Handle one of this provider's buttons."""

        raise NotImplementedError
