"""A provider's slice of the shared /data/state.json, with the same API as ``Store``."""

from __future__ import annotations

from core.store import Store


class ScopedStore:
    """``store.data[name]`` seen as a store of its own: providers keep their old section names inside it."""

    def __init__(self, store: Store, name: str) -> None:
        self._store = store
        self._name = name

    @property
    def data(self) -> dict[str, object]:
        """The provider's top-level mapping (created on first use)."""

        return self._store.section(self._name)

    def section(self, name: str) -> dict[str, object]:
        """Return (creating if needed) a mapping inside the provider's slice."""

        data = self.data
        value = data.get(name)
        if not isinstance(value, dict):
            value = {}
            data[name] = value
        return value

    @property
    def dirty(self) -> bool:
        """Whether the shared document has unsaved changes."""

        return self._store.dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._store.dirty = value

    def save(self) -> None:
        """Write the shared document if it changed."""

        self._store.save()
