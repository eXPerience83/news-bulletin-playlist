"""Production-like recovery drill: pre-provision four portable IDs, then migrate held.

No old or new Spotify API, tokens or production files are used. This documents
a working owner-controlled migration path, not permission to deploy.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from _legacy_dataset import legacy_root

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.inspection_fence import startup_gate
from news_bulletin_playlist.legacy_preflight import legacy_preflight
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import (
    ManagedStateStore,
    compile_engine_config,
    effective_sources,
)
from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle_v2
from news_bulletin_playlist.playlist_import import PlaylistImportService
from news_bulletin_playlist.portable_identity import portable_id
from news_bulletin_playlist.portable_playlists import PlaylistExportService, PortableCoverLoader
from news_bulletin_playlist.portable_sources import parse_sources
from news_bulletin_playlist.source_admin import export_source_configuration
from news_bulletin_playlist.source_import import SourceImportService
from news_bulletin_playlist.upgrade_inspection_report import upgrade_inspection_report
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("The cutover/export/restore drill must not use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "getaddrinfo", forbidden)


@pytest.mark.parametrize("legacy_schema", [6, 7, 8, 9, 10])
def test_prepare_in_old_version_then_held_v11_zip_v2_and_all_sources_are_restorable(
    tmp_path: Path, legacy_schema: int
) -> None:
    # Emulate *existing* v0.2.3b1 Admin "Prepare portable export identity"
    # BEFORE the v11 first boot. This only changes the legacy local UUIDs.
    root, _ = legacy_root(tmp_path, legacy_schema)
    old_store = ManagedStateStore(root / "managed-state.json")
    old_runtime = compile_engine_config(BUILTIN_CATALOG, old_store.load_readonly())
    old_exporter = PlaylistExportService(
        old_store, ConfigurationSynchronization(), PortableCoverLoader(root)
    )
    allocated: dict[str, str] = {}
    for playlist in old_store.load_readonly().playlists:
        playlist_id = str(playlist.id)
        fingerprint, _, portable = old_exporter.preparation("managed", playlist_id)
        assert portable is None
        allocated[playlist_id] = old_exporter.prepare(
            "managed", playlist_id, fingerprint, acknowledge_v9=True
        )
        assert portable_id(allocated[playlist_id]) == allocated[playlist_id]
    assert len(allocated) == len(set(allocated.values())) == 4
    prepared_state = old_store.load_readonly()
    assert compile_engine_config(BUILTIN_CATALOG, prepared_state) == old_runtime
    assert {str(p.id): p.portable_id for p in prepared_state.playlists} == allocated
    original_destinations = {str(p.id): p.destination.external_id for p in prepared_state.playlists}
    preflight = legacy_preflight(root, expected_destinations=4)
    assert preflight["destination_count_matches_expectation"] is True

    # The irreversible v11 first boot is held *before* data conversion.
    startup_gate(root, requested=True)
    migrated = open_managed_state_store(root)
    held = upgrade_inspection_report(root)
    assert held["gate"] == "held"
    assert held["legacy_comparison"] == "matching"
    assert held["safe_to_review_without_spotify_writes"] is True
    assert held["historical_origin_sha256"] == preflight["legacy_state_sha256"]
    assert held["sources"] == preflight["sources"]
    assert held["playlists"] == preflight["playlists"]
    assert held["owner_templates"] == preflight["owner_templates"]

    reopened = migrated.load_readonly()
    assert {str(p.id): p.portable_id for p in reopened.playlists} == allocated
    assert {str(p.id): p.destination.external_id for p in reopened.playlists} == (
        original_destinations
    )
    assert compile_engine_config(migrated.catalog, reopened) == old_runtime

    # Both exports are GET/read-only compatible. No held Admin POST is needed.
    before_v11 = (root / V11_OPERATIONAL_FILENAME).read_bytes()
    source_document = export_source_configuration(migrated, ConfigurationSynchronization())
    source_rows = parse_sources(source_document)
    assert {row.source.id for row in source_rows} == {
        src.id for src in effective_sources(migrated.catalog, reopened)
    }
    assert len(source_rows) >= 13
    exporter = PlaylistExportService(
        migrated, ConfigurationSynchronization(), PortableCoverLoader(root)
    )
    bundles = []
    for current in reopened.playlists:
        identity, raw_zip = exporter.export("managed", str(current.id), version=2)
        assert identity == allocated[str(current.id)]
        assert current.destination.external_id.encode() not in raw_zip
        parsed = inspect_playlist_bundle_v2(raw_zip)
        assert parsed.playlist["source_ids"] == list(current.source_ids)
        assert parsed.template is not None
        assert parsed.template_jpeg is not None
        assert parsed.playlist["portable_id"] == identity
        bundles.append((current, parsed, raw_zip))
    assert len(bundles) == 4
    assert (root / V11_OPERATIONAL_FILENAME).read_bytes() == before_v11
    assert upgrade_inspection_report(root) == held

    # A totally fresh root has no configured presets or destinations.
    # Restore the independently downloaded global JSON + four complete ZIP v2.
    other = tmp_path / "clean-import"
    other.mkdir()
    receiver = open_managed_state_store(other)
    assert not receiver.catalog.sources
    assert not receiver.catalog.playlists
    sync = ConfigurationSynchronization()
    pending = ProvisioningJournal(other / "managed-playlist-provisioning.json")
    imported_sources = SourceImportService(receiver, sync, pending)
    source_plan = imported_sources.preview(source_document)
    import_result = imported_sources.apply(
        source_document,
        source_plan.fingerprint,
        keep_local=frozenset(),
        confirm_v8=False,
    )
    assert import_result.created == len(source_rows)
    importer = PlaylistImportService(receiver, sync, pending, PortableCoverLoader(other))
    for old_playlist, parsed, raw_zip in bundles:
        preview = importer.preview_v2(raw_zip)
        assert not preview.blocked, preview.reason
        result = importer.apply_v2(raw_zip, preview.sources.fingerprint)
        assert not result.recovered
        assert result.current_draft_id != result.owner_template_id
        # ZIP alone never silently creates or relinks a Spotify destination.
        assert receiver.load_readonly().playlists == ()
        assert (
            old_playlist.destination.external_id.encode()
            not in (other / V11_OPERATIONAL_FILENAME).read_bytes()
        )
        restored = next(
            d
            for d in receiver.load_readonly().templates
            if d.portable_id == parsed.playlist["portable_id"]
        )
        assert restored.default_source_ids
        assert restored.retention_hours == old_playlist.retention_hours
        assert restored.min_duration_seconds == old_playlist.min_duration_seconds
        assert restored.max_duration_seconds == old_playlist.max_duration_seconds
        assert importer.covers.load(restored.cover_id)[0] == parsed.jpeg
    assert len(receiver.catalog.playlists) == 4
    assert len(receiver.load_readonly().templates) == 4
    assert len(receiver.catalog.sources) == len(source_rows)
