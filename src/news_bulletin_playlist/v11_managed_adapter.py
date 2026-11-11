"""Forward-only managed-state adapter shared by runtime and Admin.

Existing installation state upgrades once from v6-v10 to owner-controlled v11.
Legacy managed-state.json remains inert. Fresh installations start directly
in v11, without creating any Spotify managed destination automatically.
"""

from __future__ import annotations

import json
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
from news_bulletin_playlist.models import PlaylistId, SourceId
from news_bulletin_playlist.owner_inventory import OwnerInventory
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

    def owner_cover_attribution(self, cover_id: str) -> dict[str, object] | None:
        """Read validated owner credit for a restored template JPEG, without writes."""
        current = parse_v11_operational(self._expected_bytes)
        encoded = dict(current.owner_inventory.cover_attributions).get(cover_id)
        return json.loads(encoded) if encoded is not None else None

    def save_owner_bundle(self, inventory: OwnerInventory, overlay: ManagedState) -> None:
        """Commit an import's owner sources, templates and projection in one v11 CAS.

        No caller may update owner catalog and overlay in separate steps.
        An interrupted save must be treated as uncertain and inspected anew.
        """
        try:
            next_bytes = self._owner.save_owner_inventory(
                inventory,
                expected_bytes=self._expected_bytes,
                managed_overlay=overlay,
            )
            parsed = parse_v11_operational(next_bytes)
            self._expected_bytes = next_bytes
            self.catalog = parsed.active_catalog
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 imported owner bundle outcome uncertain") from exc

    def save_owner_source(
        self, edited: InstalledSource, *, reconcile_public_release_override: bool = False
    ) -> None:
        """CAS the owner definition, optionally retiring a legacy mode override atomically."""
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
            overlay = (
                replace(
                    current.managed_overlay,
                    source_overrides=tuple(
                        item
                        for item in current.managed_overlay.source_overrides
                        if item.source_id != edited.id
                    ),
                )
                if reconcile_public_release_override
                else None
            )
            next_bytes = self._owner.save_owner_inventory(
                inventory, expected_bytes=self._expected_bytes, managed_overlay=overlay
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

    def delete_owner_template(
        self,
        template_id: PlaylistId,
        *,
        pending_template_ids: tuple[PlaylistId, ...] = (),
    ) -> None:
        """Remove an unreferenced owner template via exact-byte CAS, never Spotify."""
        try:
            current = parse_v11_operational(self._expected_bytes)
            if current.managed_overlay.quarantined_templates:
                raise ManagedStateError(
                    "Repair quarantined drafts before deleting historical templates"
                )
            updated = current.owner_inventory.templates.remove_unreferenced(
                template_id,
                managed_template_ids=(
                    *tuple(playlist.template_id for playlist in current.managed_overlay.playlists),
                    *tuple(
                        item.owner_template_id
                        for item in current.managed_overlay.templates
                        if item.owner_template_id is not None
                    ),
                ),
                pending_template_ids=pending_template_ids,
            )
            inventory = replace(current.owner_inventory, templates=updated)
            next_bytes = self._owner.save_owner_inventory(
                inventory, expected_bytes=self._expected_bytes
            )
            self._expected_bytes = next_bytes
            self.catalog = parse_v11_operational(next_bytes).active_catalog
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 owner template could not be deleted safely") from exc

    def delete_owner_source(
        self, source_id: SourceId, *, pending_source_ids: tuple[SourceId, ...] = ()
    ) -> None:
        """Remove an unreferenced historic source and its order entry atomically."""
        try:
            current = parse_v11_operational(self._expected_bytes)
            state = current.managed_overlay
            inventory = current.owner_inventory
            if source_id not in {item.id for item in current.active_catalog.sources}:
                raise ManagedStateError("historical owner source was not found")
            if (
                state.quarantined_templates
                or source_id in pending_source_ids
                or any(source_id in playlist.source_ids for playlist in state.playlists)
                or any(override.source_id == source_id for override in state.source_overrides)
                or any(
                    source_id in template.default_source_ids
                    or any(e.source_id == source_id for e in template.duration_exceptions)
                    for template in state.templates
                )
                or any(
                    source_id in template.default_source_ids
                    or any(e.source_id == source_id for e in template.duration_policy.exceptions)
                    for template in inventory.templates.records
                )
            ):
                raise ManagedStateError("historical owner source still has references")
            records = tuple(item for item in inventory.sources if item.id != source_id)
            order = inventory.source_order.after_records_change(inventory.sources, records)
            next_bytes = self._owner.save_owner_inventory(
                replace(inventory, sources=records, source_order=order),
                expected_bytes=self._expected_bytes,
            )
            self._expected_bytes = next_bytes
            self.catalog = parse_v11_operational(next_bytes).active_catalog
        except (V11StoreError, OSError, ValueError) as exc:
            raise ManagedStateError("v11 owner source could not be deleted safely") from exc


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
