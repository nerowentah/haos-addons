"""Test helpers: build the merged app from one provider's options, written as the old app had them."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import config as app_config
import main
from core import store as core_store
from core import supervisor as core_supervisor
from providers.base import Engine


def merged(provider: str, raw: dict[str, object]) -> dict[str, object]:
    """``raw`` (an old app's options) renamed into this app's ``<provider>_`` keys; shared switches stay."""

    return {key if key in app_config.SHARED_KEYS else f"{provider}_{key}": value for key, value in raw.items()}


def load(provider: str, raw: dict[str, object]) -> app_config.Config:
    """The merged config for one provider's old-style options."""

    return app_config.load_config(merged(provider, raw))


def app(
    provider: str,
    raw: dict[str, object],
    supervisor: core_supervisor.Supervisor,
    store: core_store.Store,
    **engine_kwargs: object,
) -> main.App:
    """The app core with one provider, entities going through the states API (no MQTT broker)."""

    core = main.App(
        load(provider, raw), supervisor, store, engine_kwargs={provider: engine_kwargs} if engine_kwargs else None
    )
    core.broker_found = False
    return core


def engine(
    provider: str,
    raw: dict[str, object],
    supervisor: core_supervisor.Supervisor,
    store: core_store.Store,
    **engine_kwargs: object,
) -> Engine:
    """Just the provider's engine (its ``host`` is the app core)."""

    return app(provider, raw, supervisor, store, **engine_kwargs).engines[provider]
