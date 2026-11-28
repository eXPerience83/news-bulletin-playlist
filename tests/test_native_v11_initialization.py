"""Native first boot, immutable origin, backward compatibility and failure guards."""

import json
import os
import socket

import pytest
from _native_v11_fixtures import old_empty_v11_bytes

from news_bulletin_playlist import v11_operational_storage as storage
from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.legacy_catalog_snapshot import (
    FrozenLegacySnapshotError,
    parse_legacy_snapshot,
)
from news_bulletin_playlist.managed_state import (
    PLAYLIST_ORDERING_SCHEMA_VERSION,
    InstalledSource,
    ManagedState,
)
from news_bulletin_playlist.owner_inventory import OwnerInventory
from news_bulletin_playlist.owner_source_order import OwnerSourceOrder
from news_bulletin_playlist.owner_template_records import OwnerTemplateSet
from news_bulletin_playlist.upgrade_inspection_report import (
    UpgradeInspectionError,
    upgrade_inspection_report,
)
from news_bulletin_playlist.v11_managed_adapter import ForwardOnlyManagedStateStore
from news_bulletin_playlist.v11_operational_codec import (
    V11OperationalError,
    build_native_v11_operational,
    parse_v11_operational,
    update_v11_operational,
    update_v11_owner_inventory,
)
from news_bulletin_playlist.v11_operational_storage import (
    V11_AUTHORITY_FILENAME,
    V11_OPERATIONAL_FILENAME,
    V11OperationalStore,
    V11StoreError,
)


def test_native_first_boot_and_restart_never_build_legacy_candidate(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Native first boot attempted legacy conversion or provider I/O")

    for name in (
        "preview_legacy_materialization",
        "build_legacy_snapshot_v2",
        "build_v11_candidate",
        "build_v11_operational_from_candidate",
    ):
        monkeypatch.setattr(storage, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    store = V11OperationalStore(tmp_path)
    first = store.initialize_fresh()
    assert first.native_origin
    assert first.owner_sources_complete
    assert first.managed_overlay == ManagedState(schema_version=PLAYLIST_ORDERING_SCHEMA_VERSION)
    assert not first.runtime.sources and not first.runtime.playlists
    assert not first.owner_inventory.templates.records
    assert not first.inherited_cover_ids
    before = store.load_bytes()
    assert {p.name for p in tmp_path.iterdir()} == {
        V11_AUTHORITY_FILENAME,
        V11_OPERATIONAL_FILENAME,
    }
    assert (tmp_path / V11_OPERATIONAL_FILENAME).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / V11_AUTHORITY_FILENAME).stat().st_mode & 0o777 == 0o600
    assert store.initialize_fresh() == first
    assert ForwardOnlyManagedStateStore(tmp_path).load_readonly() == first.managed_overlay
    assert store.load_bytes() == before


def test_native_interrupted_publication_cannot_be_reclaimed(tmp_path, monkeypatch):
    original_link = os.link

    def fail_state_link(src, dst, *args, **kwargs):
        if dst == V11_OPERATIONAL_FILENAME:
            raise OSError("injected native publication failure")
        return original_link(src, dst, *args, **kwargs)

    store = V11OperationalStore(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(os, "link", fail_state_link)
        with pytest.raises(V11StoreError, match="fresh initialization failed"):
            store.initialize_fresh()
    assert (tmp_path / V11_AUTHORITY_FILENAME).exists()
    assert not (tmp_path / V11_OPERATIONAL_FILENAME).exists()
    with pytest.raises(V11StoreError, match="document is missing"):
        store.initialize_fresh()
    assert not (tmp_path / V11_OPERATIONAL_FILENAME).exists()


def test_native_origin_is_distinct_and_preserved_through_owner_revision():
    raw = build_native_v11_operational()
    assert raw == build_native_v11_operational()
    first = parse_v11_operational(raw)
    assert first.native_origin
    document = json.loads(raw)
    witness = document["installed_catalog"]
    with pytest.raises(FrozenLegacySnapshotError):
        parse_legacy_snapshot(json.dumps(witness).encode())
    source = InstalledSource.from_definition(BUILTIN_CATALOG.sources[0])
    inventory = OwnerInventory(OwnerSourceOrder((source.id,)), (source,), OwnerTemplateSet(()))
    updated = update_v11_owner_inventory(raw, inventory)
    reopened = parse_v11_operational(updated)
    assert reopened.native_origin
    assert reopened.owner_inventory == inventory
    assert reopened.historical_catalog == first.historical_catalog
    assert reopened.migration_origin_sha256 == first.migration_origin_sha256
    assert json.loads(updated)["installed_catalog"] == witness
    assert reopened.managed_overlay.schema_version == PLAYLIST_ORDERING_SCHEMA_VERSION
    assert update_v11_owner_inventory(updated, inventory) == updated
    assert update_v11_operational(updated, reopened.managed_overlay) == updated


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc["installed_catalog"].update(version=True),
        lambda doc: doc["installed_catalog"].update(managed_schema_version=10),
        lambda doc: doc["installed_catalog"].update(sources=[]),
        lambda doc: doc.update(migration_origin_sha256="0" * 64),
        lambda doc: doc.pop("owner_source_catalog"),
        lambda doc: doc.update(inherited_cover_ids=[]),
        lambda doc: doc["managed_overlay"].update(schema_version=10),
    ],
)
def test_native_origin_fails_closed_on_inconsistent_fields(change):
    document = json.loads(build_native_v11_operational())
    change(document)
    with pytest.raises(V11OperationalError):
        parse_v11_operational((json.dumps(document, sort_keys=True, indent=2) + "\n").encode())


def test_old_empty_v11_authority_remains_readable_and_unmodified(tmp_path):
    raw = old_empty_v11_bytes()
    decoded = parse_v11_operational(raw)
    assert not decoded.native_origin
    assert decoded.managed_overlay == ManagedState(schema_version=10)
    assert update_v11_operational(raw, decoded.managed_overlay) == raw
    path = tmp_path / V11_OPERATIONAL_FILENAME
    path.write_bytes(raw)
    path.chmod(0o600)
    assert V11OperationalStore(tmp_path).initialize_fresh() == decoded
    assert path.read_bytes() == raw


def test_native_inspection_rejects_unexpected_legacy_file(tmp_path):
    store = V11OperationalStore(tmp_path)
    store.initialize_fresh()
    before = store.load_bytes()
    assert upgrade_inspection_report(tmp_path)["legacy_comparison"] == "not_applicable"
    legacy = tmp_path / "managed-state.json"
    legacy.write_bytes(b'{"schema_version":10}')
    legacy.chmod(0o600)
    with pytest.raises(UpgradeInspectionError, match="Unexpected legacy"):
        upgrade_inspection_report(tmp_path)
    assert store.load_bytes() == before
