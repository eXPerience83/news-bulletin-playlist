"""End-to-end legacy→v11→portable interchange, excluding Spotify destination recovery.

This is deliberately NOT a proof that imported ZIPs are live managed playlists:
the current supported importer creates local drafts without the old remote IDs.
"""

from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path

import pytest
from _installed_fixtures import _installed
from _legacy_dataset import legacy_root

from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_provisioning import ProvisioningJournal
from news_bulletin_playlist.managed_state import ManagedStateStore, effective_sources
from news_bulletin_playlist.playlist_bundle import (
    inspect_playlist_bundle,
    inspect_playlist_bundle_v2,
)
from news_bulletin_playlist.playlist_import import PlaylistImportService
from news_bulletin_playlist.portable_playlists import PlaylistExportService, PortableCoverLoader
from news_bulletin_playlist.portable_sources import PortableSource, export_sources, parse_sources
from news_bulletin_playlist.source_import import SourceImportService
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        pytest.fail("Portability acceptance must never contact Spotify or other networks")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_four_legacy_playlists_and_all_sources_remain_portable_after_v11(
    tmp_path: Path, schema: int
) -> None:
    donor, legacy = legacy_root(tmp_path, schema)
    original_legacy_json = (donor / "managed-state.json").read_bytes()
    original_private_files = {
        name: (donor / name).read_bytes()
        for name in ("oauth-private", "history.sqlite3", "pending-post.json")
    }

    # A single direct migration; the image's original catalogue is frozen in
    # /data and the existing runtime, source order and destination IDs survive.
    original_runtime = legacy.original_runtime
    store = open_managed_state_store(donor)
    assert store.catalog.sources
    assert store.load_readonly() == replace(legacy.original_state, sources=())
    assert tuple(s.id for s in effective_sources(store.catalog, store.load())) == tuple(
        s.id for s in original_runtime.sources
    )
    assert tuple(p.destination.external_id for p in store.load().playlists) == tuple(
        p.destination.external_id for p in legacy.original_state.playlists
    )

    synchronization = ConfigurationSynchronization()
    exporter = PlaylistExportService(store, synchronization, PortableCoverLoader(donor))

    # Read-only source JSON covers ALL effective sources, not only the ones
    # referenced by the four playlists; caller must save this separately.
    effective = effective_sources(store.catalog, store.load_readonly())
    source_json = export_sources(
        effective, built_in_ids={source.id for source in store.catalog.sources}
    )
    parsed = parse_sources(source_json)
    assert {source.source.id for source in parsed} == {source.id for source in effective}
    assert len(parsed) == 13

    archives: dict[str, bytes] = {}
    for playlist in legacy.original_state.playlists:
        identifier = str(playlist.id)
        fingerprint, _, identity = exporter.preparation("managed", identifier)
        assert identity is None
        assert exporter.prepare("managed", identifier, fingerprint, acknowledge_v9=True)
        _, archive = exporter.export("managed", identifier)
        bundle = inspect_playlist_bundle(archive)
        assert bundle.playlist["kind"] == "managed"
        assert bundle.playlist["configuration"]["enabled"] is playlist.enabled
        assert bundle.playlist["source_ids"] == list(playlist.source_ids)
        for private_bytes in original_private_files.values():
            assert private_bytes not in archive
        assert playlist.destination.external_id.encode() not in archive
        archives[identifier] = archive

    assert len(archives) == 4
    assert (donor / "managed-state.json").read_bytes() == original_legacy_json
    for name, original_bytes in original_private_files.items():
        assert (donor / name).read_bytes() == original_bytes

    # A pristine v11 installation has zero built-in configured sources and
    # zero managed destinations; import the global sources and four ZIPs.
    recipient = tmp_path / "recipient"
    recipient.mkdir()
    restored = open_managed_state_store(recipient)
    assert restored.catalog.sources == ()
    assert restored.catalog.playlists == ()
    assert restored.load_readonly().playlists == ()
    target_sync = ConfigurationSynchronization()
    journal = ProvisioningJournal(recipient / "managed-playlist-provisioning.json")
    sources = SourceImportService(restored, target_sync, journal)
    source_preview = sources.preview(source_json)
    assert (
        sources.apply(
            source_json,
            source_preview.fingerprint,
            keep_local=frozenset(),
            confirm_v8=False,
        ).created
        == 13
    )
    playlists = PlaylistImportService(
        restored, target_sync, journal, PortableCoverLoader(recipient)
    )

    for archive in archives.values():
        preview = playlists.preview(archive)
        assert preview.status == "create"
        result = playlists.apply(
            archive,
            preview.sources.fingerprint,
            keep_local=preview.sources.conflicts,
            confirm_schema=False,
        )
        assert result.created_draft

    before = {source.external_references[0].external_id: source for source in effective}
    after = {
        source.external_references[0].external_id: source
        for source in effective_sources(restored.catalog, restored.load_readonly())
    }
    assert set(before) == set(after)
    for show_id, original in before.items():
        assert replace(after[show_id], id=original.id) == original

    final = restored.load_readonly()
    assert len(final.templates) == len(archives)
    assert not final.sources
    assert len(restored.catalog.sources) == len(effective)
    assert not final.playlists  # Current portable import does NOT rebind Spotify.
    local_sources = {source.id: source for source in effective_sources(restored.catalog, final)}
    originals = {str(p.id): p for p in legacy.original_state.playlists}
    for identifier, archive in archives.items():
        bundle = inspect_playlist_bundle(archive)
        original = originals[identifier]
        candidate = next(
            template
            for template in final.templates
            if template.portable_id == bundle.playlist["portable_id"]
        )
        assert candidate.display_name == original.display_name
        assert candidate.description == original.description
        assert candidate.retention_hours == original.retention_hours
        assert candidate.max_episodes == original.max_episodes
        assert candidate.min_duration_seconds == original.min_duration_seconds
        assert candidate.max_duration_seconds == original.max_duration_seconds
        assert tuple(
            local_sources[source_id].external_references[0].external_id
            for source_id in candidate.default_source_ids
        ) == tuple(
            next(s for s in effective if s.id == source_id).external_references[0].external_id
            for source_id in original.source_ids
        )
        assert playlists.covers.load(candidate.cover_id)[0] == bundle.jpeg


@pytest.mark.parametrize("schema", [6, 7, 8, 9, 10])
def test_four_direct_upgrade_zip_v2_imports_preserve_owner_template_closure_offline(
    tmp_path: Path, schema: int
) -> None:
    """Recover all four independently, including dormant owner policy, without Spotify."""
    donor, evidence = legacy_root(tmp_path, schema)
    before_legacy = (donor / "managed-state.json").read_bytes()
    store = open_managed_state_store(donor)
    exporter = PlaylistExportService(
        store, ConfigurationSynchronization(), PortableCoverLoader(donor)
    )
    original = evidence.original_state
    assert len(original.playlists) == 4

    archives = []
    for playlist in original.playlists:
        fingerprint, _, identity = exporter.preparation("managed", str(playlist.id))
        assert identity is None
        exporter.prepare("managed", str(playlist.id), fingerprint, acknowledge_v9=True)
        _, payload = exporter.export("managed", str(playlist.id), version=2)
        bundle = inspect_playlist_bundle_v2(payload)
        assert bundle.template is not None
        assert bundle.template_jpeg is not None
        assert bundle.template_attribution is not None
        assert bundle.playlist["source_ids"] == list(playlist.source_ids)
        assert playlist.destination.external_id.encode() not in payload
        archives.append((playlist, bundle, payload))
    assert len(archives) == 4
    assert (donor / "managed-state.json").read_bytes() == before_legacy

    recipient = tmp_path / "recipient-v2"
    recipient.mkdir()
    fresh = open_managed_state_store(recipient)
    assert not fresh.catalog.sources and not fresh.catalog.playlists
    import_service = PlaylistImportService(
        fresh,
        ConfigurationSynchronization(),
        ProvisioningJournal(recipient / "managed-playlist-provisioning.json"),
        PortableCoverLoader(recipient),
    )
    created = []
    for original_playlist, bundle, payload in archives:
        preview = import_service.preview_v2(payload)
        assert not preview.blocked, preview.reason
        assert preview.owner_template is not None
        assert len(preview.owner_template.duration_policy.exceptions) == len(
            bundle.template.duration_policy.exceptions
        )
        result = import_service.apply_v2(payload, preview.sources.fingerprint)
        assert result.created_sources == preview.sources.creates
        created.append(result)
        assert not fresh.load_readonly().playlists
        # A restored ZIP may never acquire an old or a new Spotify destination.
        assert (
            original_playlist.destination.external_id.encode()
            not in (recipient / "managed-state-v11.json").read_bytes()
        )

    assert len(fresh.catalog.playlists) == 4
    assert len({item.owner_template_id for item in fresh.load_readonly().templates}) == 4
    assert sum(item.created_sources for item in created) == len(fresh.catalog.sources)
    assert not fresh.load_readonly().playlists
    effective = {
        source.id: source for source in effective_sources(fresh.catalog, fresh.load_readonly())
    }
    by_template = {item.id: item for item in fresh.catalog.playlists}
    drafts = fresh.load_readonly().templates
    assert len(drafts) == 4

    for (original_playlist, original_bundle, _), result in zip(archives, created, strict=True):
        draft = next(item for item in drafts if str(item.id) == result.current_draft_id)
        owner = by_template[draft.owner_template_id]
        historical = original_bundle.template
        assert historical is not None
        assert draft.display_name == original_playlist.display_name
        assert draft.description == original_playlist.description
        assert draft.retention_hours == original_playlist.retention_hours
        assert draft.max_episodes == original_playlist.max_episodes
        assert draft.min_duration_seconds == original_playlist.min_duration_seconds
        assert draft.max_duration_seconds == original_playlist.max_duration_seconds
        assert len(owner.duration_policy.exceptions) == len(historical.duration_policy.exceptions)
        assert tuple(
            (e.id, e.max_seconds, e.edition_local_time, e.edition_local_time.fold)
            for e in owner.duration_policy.exceptions
        ) == tuple(
            (e.id, e.max_seconds, e.edition_local_time, e.edition_local_time.fold)
            for e in historical.duration_policy.exceptions
        )
        donor_sources = {
            source.id: source for source in effective_sources(store.catalog, store.load_readonly())
        }
        assert tuple(
            effective[id_].external_references for id_ in owner.default_source_ids
        ) == tuple(donor_sources[id_].external_references for id_ in historical.default_source_ids)
        assert import_service.covers.load(draft.cover_id)[0] == original_bundle.jpeg
        assert import_service.covers.load(owner.cover_id)[0] == original_bundle.template_jpeg


def test_v2_reuses_promoted_historical_custom_source_with_original_identity(
    tmp_path: Path,
) -> None:
    """A legacy custom station is reused by ZIP v2 from the owner catalog, not duplicated."""
    donor_parent = tmp_path / "donor-dataset"
    recipient_parent = tmp_path / "recipient-dataset"
    donor_parent.mkdir()
    recipient_parent.mkdir()
    donor, _ = legacy_root(donor_parent, 10)
    recipient, _ = legacy_root(recipient_parent, 10)
    station = _installed()
    for root, selected in ((donor, True), (recipient, False)):
        old = ManagedStateStore(root / "managed-state.json")
        state = old.load_readonly()
        playlists = (
            (
                replace(
                    state.playlists[0],
                    source_ids=(*state.playlists[0].source_ids, station.id),
                ),
                *state.playlists[1:],
            )
            if selected
            else state.playlists
        )
        old.save(replace(state, sources=(station,), playlists=playlists))
    original_recipient = (recipient / "managed-state.json").read_bytes()
    donor_store = open_managed_state_store(donor)
    exporter = PlaylistExportService(
        donor_store, ConfigurationSynchronization(), PortableCoverLoader(donor)
    )
    playlist_id = str(donor_store.load_readonly().playlists[0].id)
    fingerprint, _, identity = exporter.preparation("managed", playlist_id)
    assert identity is None
    exporter.prepare("managed", playlist_id, fingerprint, acknowledge_v9=True)
    _, archive = exporter.export("managed", playlist_id, version=2)
    bundle = inspect_playlist_bundle_v2(archive)
    assert station.spotify_show_id in {
        record.source.external_references[0].external_id
        for record in bundle.sources
        if isinstance(record, PortableSource)
    }

    restored = open_managed_state_store(recipient)
    before = restored.load_readonly()
    assert before.sources == ()
    assert len(before.playlists) == 4
    assert station.id in {source.id for source in restored.catalog.sources}
    importer = PlaylistImportService(
        restored,
        ConfigurationSynchronization(),
        ProvisioningJournal(recipient / "managed-playlist-provisioning.json"),
        PortableCoverLoader(recipient),
    )
    plan = importer.preview_v2(archive)
    historical = next(
        row for row in plan.sources.rows if row.spotify_show_id == station.spotify_show_id
    )
    assert historical.status == "identical"
    assert historical.local_id == station.id
    assert not plan.blocked
    result = importer.apply_v2(archive, plan.sources.fingerprint)
    assert result.created_sources == 0
    assert restored.load_readonly().playlists == before.playlists
    assert len(restored.catalog.sources) == 14
    assert sum(source.id == station.id for source in restored.catalog.sources) == 1
    assert (recipient / "managed-state.json").read_bytes() == original_recipient
