"""Byte-for-byte migrated cover resilience: no image bundles or Spotify calls after cutover."""

from __future__ import annotations

import socket
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from _legacy_dataset import legacy_root as _legacy_root

from news_bulletin_playlist.catalog import BUILTIN_CATALOG, BuiltInCatalog
from news_bulletin_playlist.managed_state import ManagedState, ManagedStateStore
from news_bulletin_playlist.migrated_covers import (
    MIGRATED_COVERS_DIR,
    MigratedCoverAssets,
    MigratedCoverError,
)
from news_bulletin_playlist.portable_playlists import PlaylistExportError, PortableCoverLoader
from news_bulletin_playlist.v11_operational_storage import (
    V11_AUTHORITY_FILENAME,
    V11_OPERATIONAL_FILENAME,
    V11OperationalStore,
    V11StoreError,
)


@pytest.fixture(autouse=True)
def forbid_operational_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        pytest.fail("v11 cover ownership contacted a provider or SQLite")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "getaddrinfo", blocked)
    monkeypatch.setattr(sqlite3, "connect", blocked)


@pytest.mark.parametrize("schema", [6, 8, 10])
def test_exact_historical_covers_survive_without_future_bundles(tmp_path: Path, schema: int):
    root, preview = _legacy_root(tmp_path, schema)
    original = (root / "managed-state.json").read_bytes()
    covers = PortableCoverLoader(root)
    required = {item.definition.cover_id for item in preview.template_snapshots} | {
        item.cover_id for item in preview.original_state.playlists
    }
    before = {item: covers.load(item) for item in required}
    installed = V11OperationalStore(root).initialize_from_legacy()
    assert installed.runtime == preview.original_runtime
    index = root / MIGRATED_COVERS_DIR / "index.json"
    assert index.is_file()
    assert index.stat().st_mode & 0o077 == 0
    assert (root / "managed-state.json").read_bytes() == original
    # Pretend the next image ships ZERO historical JPEGs.
    future = PortableCoverLoader(root, bundled_roots=(tmp_path / "no-bundled-assets",))
    for cover_id, (jpeg, attribution) in before.items():
        assert future.load(cover_id) == (jpeg, attribution)
    assert V11OperationalStore(root).initialize_from_legacy() == installed
    assert index.is_file()
    assert not (root / MIGRATED_COVERS_DIR / "temporary").exists()


def test_corrupt_owner_asset_never_falls_back_to_bundled_resource(tmp_path: Path):
    root, _ = _legacy_root(tmp_path)
    V11OperationalStore(root).initialize_from_legacy()
    record = MigratedCoverAssets(root)
    original = record.load("spain_spanish_news")
    assert original is not None
    asset = record._read_index()["spain_spanish_news"][0]
    stored = root / "custom-covers" / f"{asset}.jpg"
    stored.write_bytes(b"corrupted")
    with pytest.raises(MigratedCoverError, match="corrupt"):
        PortableCoverLoader(root).load("spain_spanish_news")


def test_malformed_private_manifest_never_reads_bundled_fallback(tmp_path: Path):
    root, _ = _legacy_root(tmp_path)
    V11OperationalStore(root).initialize_from_legacy()
    index = root / MIGRATED_COVERS_DIR / "index.json"
    index.write_bytes(b'{"format":"wrong","covers":[],"version":1}\n')
    with pytest.raises(MigratedCoverError):
        PortableCoverLoader(root).load("spain_spanish_news")


def test_missing_historical_cover_blocks_cutover_before_authority(tmp_path: Path):
    root = tmp_path / "legacy"
    root.mkdir()
    template = replace(BUILTIN_CATALOG.playlists[0], cover_id="removed_image_not_installed")
    custom = BuiltInCatalog(BUILTIN_CATALOG.sources, (template,))
    store = ManagedStateStore(root / "managed-state.json", catalog=custom)
    store.save(ManagedState(schema_version=6))
    before = store.path.read_bytes()
    with pytest.raises(V11StoreError, match="initialization failed"):
        V11OperationalStore(root).initialize_from_legacy(catalog=custom)
    assert store.path.read_bytes() == before
    assert not (root / V11_AUTHORITY_FILENAME).exists()
    assert not (root / V11_OPERATIONAL_FILENAME).exists()


def test_fresh_v11_installs_no_curated_cover_assets(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    V11OperationalStore(root).initialize_fresh()
    assert not (root / MIGRATED_COVERS_DIR).exists()
    assert not (root / "custom-covers").exists()
    with pytest.raises(PlaylistExportError):
        PortableCoverLoader(root, bundled_roots=(tmp_path / "missing",)).load("spain_spanish_news")
