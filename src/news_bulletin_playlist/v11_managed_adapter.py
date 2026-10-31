"""Forward-only managed-state adapter shared by runtime and Admin.

Existing installation state upgrades once from v6-v10 to owner-controlled v11.
Legacy managed-state.json remains inert. Fresh installations start directly
in v11, without creating any Spotify managed destination automatically.
"""

from __future__ import annotations

from pathlib import Path

from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
)
from news_bulletin_playlist.v11_operational_storage import (
    V11_OPERATIONAL_FILENAME,
    V11OperationalStore,
    V11StoreError,
)


class ForwardOnlyManagedStateStore(ManagedStateStore):
    """Drop-in facade for validated read/write schema-v11 configuration."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir / MANAGED_STATE_FILENAME)
        self._owner = V11OperationalStore(data_dir)
        legacy = data_dir / MANAGED_STATE_FILENAME
        owned_path = data_dir / V11_OPERATIONAL_FILENAME
        try:
            if (
                legacy.exists()
                or legacy.is_symlink()
                or owned_path.exists()
                or owned_path.is_symlink()
            ):
                owned = self._owner.initialize_from_legacy(catalog=self.catalog)
            else:
                owned = self._owner.initialize_fresh(catalog=self.catalog)
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("forward-only v11 conversion failed") from exc
        self.catalog = owned.historical_catalog

    def load(self) -> ManagedState:
        return self.load_readonly()

    def load_readonly(self) -> ManagedState:
        try:
            return self._owner.load().managed_overlay
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 owner state is missing or corrupt") from exc

    def save(self, state: ManagedState) -> None:
        try:
            expected = self._owner.load_bytes()
            self._owner.save(state, expected_bytes=expected)
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 owner state could not be saved") from exc


def open_managed_state_store(data_dir: Path) -> ManagedStateStore:
    """Use schema-v11 on fresh and existing installations alike."""
    return ForwardOnlyManagedStateStore(data_dir)
