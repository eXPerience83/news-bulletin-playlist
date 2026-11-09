"""Forward-only managed-state adapter shared by runtime and Admin.

Existing installation state upgrades once from v6-v10 to owner-controlled v11.
Legacy managed-state.json remains inert. Fresh installations start directly
in v11, without creating any Spotify managed destination automatically.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from news_bulletin_playlist.catalog import PlaylistTemplate
from news_bulletin_playlist.managed_state import (
    MANAGED_STATE_FILENAME,
    InstalledSource,
    ManagedState,
    ManagedStateError,
    ManagedStateStore,
)
from news_bulletin_playlist.v11_operational_codec import parse_v11_operational
from news_bulletin_playlist.v11_operational_storage import (
    V11_AUTHORITY_FILENAME,
    V11_OPERATIONAL_FILENAME,
    V11OperationalStore,
    V11StoreError,
)


class ForwardOnlyManagedStateStore(ManagedStateStore):
    """Drop-in facade for validated read/write schema-v11 configuration."""

    def __init__(self, data_dir: Path, *, readonly: bool = False) -> None:
        super().__init__(data_dir / MANAGED_STATE_FILENAME)
        self._owner = V11OperationalStore(data_dir)
        legacy = data_dir / MANAGED_STATE_FILENAME
        owned_path = data_dir / V11_OPERATIONAL_FILENAME
        try:
            if readonly:
                raw = self._owner.load_bytes()
                owned = parse_v11_operational(raw)
            elif (
                legacy.exists()
                or legacy.is_symlink()
                or owned_path.exists()
                or owned_path.is_symlink()
                or (data_dir / V11_AUTHORITY_FILENAME).exists()
                or (data_dir / V11_AUTHORITY_FILENAME).is_symlink()
            ):
                owned = self._owner.initialize_from_legacy(catalog=self.catalog)
                raw = self._owner.load_bytes()
            else:
                owned = self._owner.initialize_fresh()
                raw = self._owner.load_bytes()
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("forward-only v11 conversion failed") from exc
        self.catalog = owned.active_catalog
        self._expected_bytes = raw

    def load(self) -> ManagedState:
        return self.load_readonly()

    def load_readonly(self) -> ManagedState:
        try:
            raw = self._owner.load_bytes()
            owned = parse_v11_operational(raw)
            self._expected_bytes = raw
            self.catalog = owned.active_catalog
            return owned.managed_overlay
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 owner state is missing or corrupt") from exc

    def save(self, state: ManagedState) -> None:
        try:
            self._expected_bytes = self._owner.save(state, expected_bytes=self._expected_bytes)
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 owner state could not be saved") from exc

    def save_owner_source(self, edited: InstalledSource) -> None:
        """Edit a historical owner source without replacing specialized processing."""
        if not isinstance(edited, InstalledSource):
            raise ManagedStateError("historical source edit requires complete metadata")
        try:
            current = parse_v11_operational(self._expected_bytes)
            if edited.id not in {item.id for item in current.active_catalog.sources}:
                raise ManagedStateError("source is not owned by historical inventory")
            original = next(
                item for item in current.owner_inventory.sources if item.id == edited.id
            )
            if (
                edited.id != original.id
                or edited.spotify_show_id != original.spotify_show_id
                or edited.parser_id != original.parser_id
                or edited.collection_filter_id != original.collection_filter_id
                or edited.timezone != original.timezone
                or edited.countries != original.countries
                or edited.languages[1:] != original.languages[1:]
                or edited.enabled != original.enabled
            ):
                raise ManagedStateError(
                    "historical source identity or detector cannot be edited here"
                )
            inventory = replace(
                current.owner_inventory,
                sources=tuple(
                    edited if item.id == edited.id else item
                    for item in current.owner_inventory.sources
                ),
            )
            next_bytes = self._owner.save_owner_inventory(
                inventory, expected_bytes=self._expected_bytes
            )
            self._expected_bytes = next_bytes
            self.catalog = parse_v11_operational(next_bytes).active_catalog
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 owner source could not be saved") from exc
    def save_owner_template(self, edited: PlaylistTemplate) -> None:
        """CAS a historical template preserving source closure and dormant exceptions."""
        if not isinstance(edited, PlaylistTemplate):
            raise ManagedStateError("historical owner template must be complete")
        try:
            current = parse_v11_operational(self._expected_bytes)
            inventory = current.owner_inventory
            templates = inventory.templates.replace_one(
                edited.id, edited, sources=inventory.source_order.ids
            )
            next_bytes = self._owner.save_owner_inventory(
                replace(inventory, templates=templates),
                expected_bytes=self._expected_bytes,
            )
            self._expected_bytes = next_bytes
            self.catalog = parse_v11_operational(next_bytes).active_catalog
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 owner template could not be saved") from exc


class ReadOnlyLegacyStateStore(ManagedStateStore):
    """Pre-cutover GET and preview may read legacy, never persist legacy edits."""

    def save(self, state: ManagedState) -> None:
        raise ManagedStateError("read-only pre-cutover legacy state cannot be saved")


def open_managed_state_store_readonly(data_dir: Path) -> ManagedStateStore:
    """Read v11 if active; otherwise avoid migrating during export/preview GET."""
    from news_bulletin_playlist.v11_operational_storage import V11_AUTHORITY_FILENAME

    if any(
        (data_dir / name).exists() or (data_dir / name).is_symlink()
        for name in (V11_OPERATIONAL_FILENAME, V11_AUTHORITY_FILENAME)
    ):
        return ForwardOnlyManagedStateStore(data_dir, readonly=True)
    return ReadOnlyLegacyStateStore(data_dir / MANAGED_STATE_FILENAME)


def open_managed_state_store(data_dir: Path) -> ManagedStateStore:
    """Use schema-v11 on fresh and existing installations alike."""
    return ForwardOnlyManagedStateStore(data_dir)
