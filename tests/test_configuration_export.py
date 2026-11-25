"""Offline complete configuration snapshot export and cross-component validation."""

import struct
from contextlib import contextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from _admin_effect_guards import forbid_effects as _forbid_effects
from _admin_http import login as _login
from _admin_http import request as _request
from _file_snapshots import snapshot_files as _files
from _legacy_dataset import legacy_root
from _playlist_template_fixtures import _draft
from _source_admin_http_fixtures import _server

from news_bulletin_playlist import configuration_export
from news_bulletin_playlist.configuration_export import (
    ConfigurationExportError,
    TemplateLink,
    export_configuration,
    inspect_configuration,
)
from news_bulletin_playlist.engine_runtime import ConfigurationSynchronization
from news_bulletin_playlist.managed_state import (
    PLAYLIST_ORDERING_SCHEMA_VERSION,
    ManagedSourceOverride,
    activate_template,
)
from news_bulletin_playlist.models import OrderingPolicy, PublicReleaseMode
from news_bulletin_playlist.owner_inventory_export import (
    export_owner_inventory,
    inspect_owner_inventory,
)
from news_bulletin_playlist.playlist_collection import inspect_collection
from news_bulletin_playlist.portable_playlists import (
    PlaylistExportError,
    PlaylistExportService,
)
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore

EXPORT = "/admin/playlists/export-configuration.zip"


def _installation(tmp_path, *, linked=False):
    root, _ = legacy_root(tmp_path, 10)
    store = open_managed_state_store(root)
    state = store.load_readonly()
    draft = _draft(
        portable_id=str(uuid4()),
        hidden=True,
        ordering_override=OrderingPolicy.LATEST_PER_SOURCE_FIRST,
        owner_template_id=(
            store.catalog.playlist("international_english_news").id if linked else None
        ),
    )
    local_managed = replace(
        activate_template(draft.as_playlist_template(), "private-local-destination"),
        portable_id=str(uuid4()),
        enabled=False,
    )
    existing = tuple(replace(p, portable_id=str(uuid4())) for p in state.playlists)
    store.save(
        replace(
            state,
            schema_version=PLAYLIST_ORDERING_SCHEMA_VERSION,
            playlists=(*existing, local_managed),
            templates=(draft,),
        )
    )
    return root, store, draft, local_managed


@pytest.mark.parametrize("linked", [False, True])
def test_complete_snapshot_exact_components_and_associations(tmp_path, linked):
    root, store, draft, local_managed = _installation(tmp_path, linked=linked)
    synchronization = ConfigurationSynchronization()
    before = _files(root)
    document = export_configuration(root, synchronization)
    result = inspect_configuration(document)
    assert export_configuration(root, synchronization) == document
    assert result.owner_document == export_owner_inventory(root, synchronization)
    assert result.collection_document == PlaylistExportService.export_all_readonly(
        root, synchronization
    )
    owner = inspect_owner_inventory(result.owner_document)
    members = inspect_collection(result.collection_document)
    assert len(members) == 6
    assert sum(member.kind == "managed" for member in members) == 5
    assert sum(member.kind == "draft" for member in members) == 1
    assert owner.inventory.templates.records == store.catalog.playlists
    assert len(result.managed_links) == 5
    assert TemplateLink(local_managed.portable_id, "draft", draft.portable_id) in (
        result.managed_links
    )
    assert sum(link.template_kind == "owner" for link in result.managed_links) == 4
    assert b"private-local-destination" not in document
    assert b"secret-oauth" not in document
    assert b"secret-history" not in document
    assert b"pending-post" not in document
    assert before == _files(root)


def test_reject_mixed_snapshot_and_invalid_associations(tmp_path):
    root, store, _, local_managed = _installation(tmp_path)
    sync = ConfigurationSynchronization()
    original = inspect_configuration(export_configuration(root, sync))
    state = store.load_readonly()
    source = next(s for s in store.catalog.sources if s.id == state.playlists[0].source_ids[0])
    mode = (
        PublicReleaseMode.WALL_TIME
        if source.public_release_mode is PublicReleaseMode.INSTANT
        else PublicReleaseMode.INSTANT
    )
    store.save(replace(state, source_overrides=(ManagedSourceOverride(source.id, mode),)))
    newer_owner = export_owner_inventory(root, sync)
    mixed = configuration_export._encode(replace(original, owner_document=newer_owner))
    with pytest.raises(ConfigurationExportError, match="source differs"):
        inspect_configuration(mixed)

    broken = tuple(
        replace(link, template_id=str(uuid4()))
        if link.playlist_uuid == local_managed.portable_id
        else link
        for link in original.managed_links
    )
    with pytest.raises(ConfigurationExportError, match="association does not match"):
        inspect_configuration(configuration_export._encode(replace(original, managed_links=broken)))


def test_empty_and_missing_authority(tmp_path):
    before = _files(tmp_path)
    sync = ConfigurationSynchronization()
    with pytest.raises(ConfigurationExportError):
        export_configuration(tmp_path, sync)
    assert before == _files(tmp_path)
    open_managed_state_store(tmp_path)
    before = _files(tmp_path)
    result = inspect_configuration(export_configuration(tmp_path, sync))
    assert not result.managed_links
    assert not inspect_collection(result.collection_document)
    owner = inspect_owner_inventory(result.owner_document)
    assert owner.inventory.sources == owner.inventory.templates.records == owner.artwork == ()
    assert before == _files(tmp_path)


@pytest.mark.parametrize("malformation", ["zip64", "directory"])
def test_outer_zip_rejected_before_parser(tmp_path, monkeypatch, malformation):
    open_managed_state_store(tmp_path)
    baseline = export_configuration(tmp_path, ConfigurationSynchronization())
    if malformation == "zip64":
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)
        document = baseline[:-42] + locator + baseline[-22:]
    else:
        offset = struct.unpack_from("<I", baseline, len(baseline) - 6)[0]
        document = baseline[:offset] + b"BAD!" + baseline[offset + 4 :]

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid central directory reached ZipFile")

    monkeypatch.setattr(configuration_export, "ZipFile", forbidden)
    with pytest.raises(ConfigurationExportError):
        inspect_configuration(document)


def test_held_http_reads_once_and_fails_without_partial_attachment(tmp_path, monkeypatch, capfd):
    root, _, _, _ = _installation(tmp_path)
    with _server(root) as (server, client, _legacy):
        handler = server.RequestHandlerClass
        assert _request(server, "GET", EXPORT)[0] in {303, 401, 403}
        cookie = _login(server)
        handler.managed_admin_service = None
        handler.inspection_hold = True
        synchronization = handler.configuration_synchronization
        original_hold = synchronization.hold
        original_load = V11OperationalStore.load_bytes
        active = False
        acquisitions = 0
        reads = 0

        @contextmanager
        def tracked_hold():
            nonlocal active, acquisitions
            assert not active
            with original_hold():
                active = True
                acquisitions += 1
                try:
                    yield
                finally:
                    active = False

        def tracked_load(store):
            nonlocal reads
            assert active
            reads += 1
            return original_load(store)

        before = _files(root)
        monkeypatch.setattr(synchronization, "hold", tracked_hold)
        monkeypatch.setattr(V11OperationalStore, "load_bytes", tracked_load)
        _forbid_effects(monkeypatch, handler.source_admin_service, server)
        status, headers, document = _request(server, "GET", EXPORT, cookie=cookie)
        assert status == 200, document
        assert headers["Cache-Control"] == "no-store"
        assert headers["Content-Type"] == "application/zip"
        assert len(inspect_configuration(document).managed_links) == 5
        assert acquisitions == reads == 1
        assert _request(server, "GET", EXPORT + "?x=1", cookie=cookie)[0] == 400

        original_collection = configuration_export.collection_from_snapshot

        def fail_after_inventory(*args, **kwargs):
            # Both components have been built; even a late failure cannot emit a partial archive.
            original_collection(*args, **kwargs)
            raise PlaylistExportError("fake-private-token")

        monkeypatch.setattr(configuration_export, "collection_from_snapshot", fail_after_inventory)
        status, headers, error = _request(server, "GET", EXPORT, cookie=cookie)
        assert status == 409
        assert "Content-Disposition" not in headers
        assert error == b"Configuration could not be exported safely"
        assert acquisitions == reads == 2
        assert before == _files(root)
        assert not client.calls
        assert handler.source_admin_service.auth.calls == 0
    captured = capfd.readouterr()
    assert "fake-private-token" not in captured.out + captured.err
