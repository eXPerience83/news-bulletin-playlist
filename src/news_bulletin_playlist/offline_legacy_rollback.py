"""Offline legacy rollback rehearsal over three independent data trees.

This is not an old-container acceptance test or a migration executor.
It never writes the source, independent backup, or restored data tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from news_bulletin_playlist.managed_state import (
    ManagedStateError,
    ManagedStateStore,
    compile_engine_config,
)
from news_bulletin_playlist.offline_dataset_inventory import (
    DatasetInventoryError,
    OfflineDatasetInventory,
    inventory_offline_dataset,
)
from news_bulletin_playlist.offline_sqlite_backup import (
    OfflineSQLiteBackupError,
    inspect_offline_sqlite_backup,
)
from news_bulletin_playlist.v11_candidate import V11CandidateError
from news_bulletin_playlist.v11_shadow_staging import (
    V11ShadowStageError,
    _require_independent_backup,
    _verified_offline_inventory,
    inspect_v11_shadow_state,
)


class OfflineLegacyRollbackError(ValueError):
    """Rollback evidence did not reproduce the original legacy configuration."""


@dataclass(frozen=True, slots=True)
class OfflineLegacyRollbackReport:
    original_backup: OfflineDatasetInventory
    restored_dataset: OfflineDatasetInventory
    managed_schema_version: int
    destination_count: int
    sqlite_table_count: int

    @property
    def proves_old_image_compatibility(self) -> bool:
        return False

    @property
    def permits_cutover(self) -> bool:
        return False


def rehearse_offline_legacy_rollback(
    source: Path,
    backup: Path,
    restored: Path,
    *,
    operator_asserts_offline: bool = False,
) -> OfflineLegacyRollbackReport:
    """Check restored legacy state against independent frozen v11 evidence.

    All three directories must exist independently. An operator assertion is
    required, but is not proof of quiescence or of any real container shutdown.
    SQLite is opened ONLY on an additional disposable copy, never in these roots.
    """
    if not operator_asserts_offline:
        raise OfflineLegacyRollbackError("explicit offline operator assertion is required")
    try:
        _require_independent_backup(source, backup)
        _require_independent_backup(source, restored)
        _require_independent_backup(backup, restored)
        original = inventory_offline_dataset(source)
        snapshot = inspect_v11_shadow_state(source, backup)
        restored_before = _verified_offline_inventory(restored, backup)
        if restored_before != snapshot.original_dataset:
            raise OfflineLegacyRollbackError("restored root differs from original backup")
        sqlite_result = inspect_offline_sqlite_backup(
            restored, backup, operator_asserts_offline=True
        )
        catalog = snapshot.state.historical_catalog
        previous = ManagedStateStore(
            restored / "managed-state.json", catalog=catalog
        ).load_readonly()
        if previous != snapshot.state.managed_overlay:
            raise OfflineLegacyRollbackError("restored legacy managed state has changed")
        if compile_engine_config(catalog, previous) != snapshot.state.runtime:
            raise OfflineLegacyRollbackError("legacy reader no longer reproduces old runtime")
        destinations = tuple(
            (item.id, item.destination.adapter_id, item.destination.external_id)
            for item in previous.playlists
        )
        expected = tuple(
            (item.id, item.destination.adapter_id, item.destination.external_id)
            for item in snapshot.state.managed_overlay.playlists
        )
        if destinations != expected:
            raise OfflineLegacyRollbackError("restored Spotify destination identities differ")
        if _verified_offline_inventory(restored, backup) != restored_before:
            raise OfflineLegacyRollbackError("restored data changed during rehearsal")
        if inventory_offline_dataset(source) != original:
            raise OfflineLegacyRollbackError("original data changed during rehearsal")
        if inspect_v11_shadow_state(source, backup).original_dataset != restored_before:
            raise OfflineLegacyRollbackError("backup evidence changed during rehearsal")
    except (
        DatasetInventoryError,
        ManagedStateError,
        OfflineSQLiteBackupError,
        V11ShadowStageError,
        V11CandidateError,
        OSError,
    ) as exc:
        raise OfflineLegacyRollbackError("offline legacy rollback evidence is invalid") from exc
    return OfflineLegacyRollbackReport(
        original_backup=restored_before,
        restored_dataset=restored_before,
        managed_schema_version=previous.schema_version,
        destination_count=len(destinations),
        sqlite_table_count=sqlite_result.schema_table_count,
    )
