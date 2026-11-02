"""Cross-service, isolated v11 acceptance (forward migration and portable round trips)."""

from __future__ import annotations

import socket
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_legacy_catalog_snapshot import _root
from test_v11_playlist_examples import _artwork

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_cover_assets import CustomCoverAssetStore
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_admin import ManagedAdminService
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import effective_sources
from news_bulletin_playlist.playlist_bundle import inspect_playlist_bundle
from news_bulletin_playlist.playlist_import import PlaylistImportService
from news_bulletin_playlist.portable_playlists import (
    PlaylistExportService,
    PortableCoverLoader,
)
from news_bulletin_playlist.portable_sources import export_sources
from news_bulletin_playlist.source_import import (
    SourceImportService,
    StaleSourceImport,
)
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11_OPERATIONAL_FILENAME


@pytest.fixture(autouse=True)
def no_provider_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("v11 acceptance attempted provider network I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_legacy_forward_migration_preserves_synthetic_wal_and_all_four_destinations(
    tmp_path: Path, schema: int
) -> None:
    root, preview = _root(tmp_path, schema)
    # A real, isolated SQLite WAL remains open across the migration.
    database = root / "synthetic-episodes.sqlite3"
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE episodes(id INTEGER PRIMARY KEY, provider TEXT)")
        conn.execute("INSERT INTO episodes(provider) VALUES ('offline synthetic')")
        conn.commit()
        assert database.with_name(database.name + "-wal").is_file()
        before = _files(root)
        store = open_managed_state_store(root)
        actual = store.load_readonly()
        assert actual == preview.original_state
        assert len(actual.playlists) == 4
        assert [p.destination.external_id for p in actual.playlists] == [
            p.destination.external_id for p in preview.original_state.playlists
        ]
        assert [(p.id, p.template_id, p.source_ids, p.enabled) for p in actual.playlists] == [
            (p.id, p.template_id, p.source_ids, p.enabled)
            for p in preview.original_state.playlists
        ]
        after = _files(root)
        for filename, data in before.items():
            assert after[filename] == data, filename
        assert V11_OPERATIONAL_FILENAME in after
        assert open_managed_state_store(root).load_readonly() == actual
        # Reopening v11 never rewrites legacy/SQLite/WAL/OAuth or provider journals.
        assert _files(root) == after
    finally:
        conn.close()


def test_fresh_v11_source_json_example_draft_zip_import_two_independent_roots(
    tmp_path: Path,
) -> None:
    donor = tmp_path / "donor"
    donor.mkdir()
    original_store = open_managed_state_store(donor)
    assert original_store.catalog.sources == original_store.catalog.playlists == ()
    assert original_store.load_readonly().sources == ()
    sync = ConfigurationSynchronization()
    journal = ProvisioningJournal(donor / "managed-playlist-provisioning.json")
    importer = SourceImportService(original_store, sync, journal)
    cnn = next(s for s in BUILTIN_CATALOG.sources if str(s.id) == "cnn")
    source_document = export_sources((cnn,), built_in_ids={cnn.id})
    first_preview = importer.preview(source_document)
    assert first_preview.rows[0].status == "create"
    assert importer.apply(
        source_document, first_preview.fingerprint,
        keep_local=frozenset(), confirm_v8=False,
    ).created == 1
    assert importer.preview(source_document).rows[0].status == "reuse"
    installed = effective_sources(original_store.catalog, original_store.load_readonly())
    assert len(installed) == 1 and installed[0].id != cnn.id

    assets = CustomCoverAssetStore(donor / "custom-covers")
    admin = ManagedAdminService(
        original_store,
        catalog=original_store.catalog,
        cover_loader=lambda _: _artwork(),
        cover_installer=assets.install_canonical,
        client_factory=lambda _: pytest.fail("Spotify must not be contacted"),
    )
    draft = admin.create_from_example("international_spanish_news")
    assert draft.default_source_ids == (installed[0].id,)
    assert draft.portable_id is not None
    assert original_store.load_readonly().playlists == ()

    exporter = PlaylistExportService(
        original_store, sync, PortableCoverLoader(donor)
    )
    portable_identity, archive = exporter.export("draft", str(draft.id))
    assert portable_identity == draft.portable_id
    bundle = inspect_playlist_bundle(archive)
    assert bundle.playlist["duration_policy"]["min_seconds"] == draft.min_duration_seconds
    assert bundle.playlist["duration_policy"]["max_seconds"] == draft.max_duration_seconds
    assert bundle.jpeg == assets.load(draft.cover_id)
    assert b"secret-oauth" not in archive
    before = _files(donor)
    assert admin.create_from_example("international_spanish_news") == draft
    assert _files(donor) == before

    recipient = tmp_path / "recipient"
    recipient.mkdir()
    imported_store = open_managed_state_store(recipient)
    receiving = PlaylistImportService(
        imported_store,
        ConfigurationSynchronization(),
        ProvisioningJournal(recipient / "managed-playlist-provisioning.json"),
        PortableCoverLoader(recipient),
    )
    assert imported_store.load_readonly().sources == ()
    preview = receiving.preview(archive)
    assert preview.status == "create" and not preview.blocked
    result = receiving.apply(
        archive, preview.sources.fingerprint,
        keep_local=preview.sources.conflicts, confirm_schema=False,
    )
    assert result.created_draft and result.created_sources == 1
    restored = open_managed_state_store(recipient).load_readonly()
    assert restored.playlists == ()
    assert len(restored.sources) == len(restored.templates) == 1
    restored_draft = restored.templates[0]
    assert restored_draft.portable_id == draft.portable_id
    assert restored_draft.display_name == draft.display_name
    assert restored_draft.description == draft.description
    assert restored_draft.min_duration_seconds == draft.min_duration_seconds
    assert restored_draft.max_duration_seconds == draft.max_duration_seconds
    assert restored_draft.retention_hours == draft.retention_hours
    assert restored_draft.max_episodes == draft.max_episodes
    assert restored_draft.default_source_ids == (restored.sources[0].id,)
    assert CustomCoverAssetStore(recipient / "custom-covers").load(
        restored_draft.cover_id
    ) == bundle.jpeg

    before_retry = _files(recipient)
    restarted = PlaylistImportService(
        open_managed_state_store(recipient),
        ConfigurationSynchronization(),
        ProvisioningJournal(recipient / "managed-playlist-provisioning.json"),
        PortableCoverLoader(recipient),
    )
    second = restarted.preview(archive)
    assert second.status == "identical"
    assert not restarted.apply(
        archive, second.sources.fingerprint,
        keep_local=second.sources.conflicts, confirm_schema=False,
    ).created_draft
    assert _files(recipient) == before_retry

    # An untrusted ZIP and a stale independent source preview must not mutate v11.
    with pytest.raises(ValueError):
        restarted.preview(archive[:-20] + b"not-a-valid-zip")
    assert _files(recipient) == before_retry
    # Valid preview becomes stale as soon as a separate owner edit is committed.
    second_source = next(s for s in BUILTIN_CATALOG.sources if str(s.id) == "ser")
    source_service = SourceImportService(
        restarted.store, restarted.synchronization, restarted.provisioning
    )
    second_document = export_sources((second_source,), built_in_ids={second_source.id})
    stale = source_service.preview(second_document)
    owned = restarted.store.load()
    restarted.store.save(replace(owned, templates=(
        replace(owned.templates[0], description="Locally edited"),
    )))
    with pytest.raises(StaleSourceImport):
        source_service.apply(
            second_document, stale.fingerprint,
            keep_local=frozenset(), confirm_v8=False,
        )
    assert len(restarted.store.load().sources) == 1
