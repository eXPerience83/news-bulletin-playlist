"""Bulk ZIP v3 export and read-only Admin integration."""

import struct
from contextlib import contextmanager
from dataclasses import replace
from http import HTTPStatus

import pytest
from _admin_effect_guards import forbid_effects as _forbid_effects
from _admin_http import login as _login
from _admin_http import request as _request
from _file_snapshots import snapshot_files as _files
from _playlist_template_fixtures import _draft
from _portable_playlist_fixtures import OTHER_UUID, _ready, _service
from _source_admin_http_fixtures import _server

from news_bulletin_playlist import playlist_collection
from news_bulletin_playlist.managed_state import ManagedState
from news_bulletin_playlist.playlist_collection import (
    CollectionError,
    build_collection,
    inspect_collection,
)
from news_bulletin_playlist.portable_playlists import PlaylistExportError, PortableCoverLoader
from news_bulletin_playlist.v11_managed_adapter import open_managed_state_store
from news_bulletin_playlist.v11_operational_storage import V11OperationalStore

EXPORT_ALL = "/admin/playlists/export-all.zip"


def _both():
    state = _ready()
    return replace(
        state,
        playlists=(replace(state.playlists[0], enabled=False),),
        templates=(_draft(portable_id=OTHER_UUID, hidden=True),),
    )


def test_all_members_exact_and_deterministic(tmp_path):
    service = _service(tmp_path, _both())
    state = service.store.load_readonly()
    individual = [
        service.export("managed", str(state.playlists[0].id), version=3)[1],
        service.export("draft", str(state.templates[0].id), version=3)[1],
    ]
    before = _files(tmp_path)
    result = service.export_all()
    assert result == service.export_all() == build_collection(reversed(individual))
    assert {item.document for item in inspect_collection(result)} == set(individual)
    assert before == _files(tmp_path)
    with pytest.raises(CollectionError, match="Duplicate"):
        build_collection([individual[0], individual[0]])
    with pytest.raises(CollectionError):
        inspect_collection(result + b"x")
    with pytest.raises(CollectionError):
        inspect_collection(result[:50] + b"x" + result[51:])


@pytest.mark.parametrize("limit", ["MAX_MEMBERS", "MAX_PAYLOAD_BYTES"])
def test_aggregate_limits_accept_exact_and_reject_one_extra(tmp_path, monkeypatch, limit):
    service = _service(tmp_path, _both())
    state = service.store.load_readonly()
    documents = [
        service.export("managed", str(state.playlists[0].id), version=3)[1],
        service.export("draft", str(state.templates[0].id), version=3)[1],
    ]
    baseline = build_collection(documents)
    exact = len(documents) if limit == "MAX_MEMBERS" else sum(map(len, documents))
    monkeypatch.setattr(playlist_collection, limit, exact)
    assert build_collection(documents) == baseline
    assert {member.document for member in inspect_collection(baseline)} == set(documents)

    # Both members remain individually valid and have distinct portable identities.
    monkeypatch.setattr(playlist_collection, limit, exact - 1)
    with pytest.raises(CollectionError, match="aggregate limits"):
        build_collection(documents)
    expected = "ZIP envelope" if limit == "MAX_MEMBERS" else "ZIP entry"
    with pytest.raises(CollectionError, match=expected):
        inspect_collection(baseline)


def test_empty_collection(tmp_path):
    service = _service(tmp_path, ManagedState(schema_version=9))
    assert inspect_collection(service.export_all()) == ()


def test_missing_identity_fails_entire_export(tmp_path):
    state = _both()
    service = _service(
        tmp_path,
        replace(
            state,
            schema_version=8,
            playlists=(replace(state.playlists[0], portable_id=None),),
            templates=(replace(state.templates[0], portable_id=None),),
        ),
    )
    before = _files(tmp_path)
    with pytest.raises(PlaylistExportError):
        service.export_all()
    assert before == _files(tmp_path)


def test_authenticated_offline_download(tmp_path, monkeypatch):
    with _server(tmp_path) as (server, client, store):
        store.save(_both())
        assert _request(server, "GET", EXPORT_ALL)[0] in {303, 401, 403}
        cookie = _login(server)
        before = _files(tmp_path)
        _forbid_effects(monkeypatch, server.RequestHandlerClass.source_admin_service, server)
        status, headers, payload = _request(server, "GET", EXPORT_ALL, cookie=cookie)
        assert status == HTTPStatus.OK
        assert headers["Content-Type"] == "application/zip"
        assert headers["Cache-Control"] == "no-store"
        assert len(inspect_collection(payload)) == 2
        assert _request(server, "GET", EXPORT_ALL + "?kind=draft", cookie=cookie)[0] == 400
        assert before == _files(tmp_path)
        assert not client.calls


@pytest.mark.parametrize("malformation", ["zip64", "directory"])
def test_reject_outer_metadata_before_zipfile(tmp_path, monkeypatch, malformation):
    baseline = _service(tmp_path, _both()).export_all()
    if malformation == "zip64":
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, 0, 1)
        document = baseline[:-42] + locator + baseline[-22:]
        expected = "ZIP64"
    else:
        offset = struct.unpack_from("<I", baseline, len(baseline) - 6)[0]
        document = baseline[:offset] + b"BAD!" + baseline[offset + 4 :]
        expected = "central directory"

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid directory reached ZipFile")

    monkeypatch.setattr(playlist_collection, "ZipFile", forbidden)
    with pytest.raises(CollectionError, match=expected):
        inspect_collection(document)


def test_v11_held_download_without_managed_service_reads_once_under_lock(tmp_path, monkeypatch):
    with _server(tmp_path) as (server, client, legacy):
        legacy.save(_both())
        owned = open_managed_state_store(tmp_path)
        assert len(owned.load_readonly().playlists) == 1
        handler = server.RequestHandlerClass
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
            assert active, "Authority read outside the configuration lock"
            reads += 1
            return original_load(store)

        before = _files(tmp_path)
        monkeypatch.setattr(synchronization, "hold", tracked_hold)
        monkeypatch.setattr(V11OperationalStore, "load_bytes", tracked_load)
        _forbid_effects(monkeypatch, handler.source_admin_service, server)
        status, headers, payload = _request(server, "GET", EXPORT_ALL, cookie=cookie)
        assert status == HTTPStatus.OK, payload
        assert headers["Content-Type"] == "application/zip"
        assert {member.kind for member in inspect_collection(payload)} == {"managed", "draft"}
        assert acquisitions == reads == 1
        assert before == _files(tmp_path)
        assert not client.calls


def test_late_cover_failure_returns_no_attachment(tmp_path, monkeypatch):
    with _server(tmp_path) as (server, client, store):
        store.save(_both())
        cookie = _login(server)
        before = _files(tmp_path)
        original_load = PortableCoverLoader.load
        seen = []

        def fail_draft_cover(loader, cover_id):
            seen.append(cover_id)
            if cover_id == _both().templates[0].cover_id:
                raise PlaylistExportError("Selected cover unavailable")
            return original_load(loader, cover_id)

        monkeypatch.setattr(PortableCoverLoader, "load", fail_draft_cover)
        _forbid_effects(monkeypatch, server.RequestHandlerClass.source_admin_service, server)
        status, headers, payload = _request(server, "GET", EXPORT_ALL, cookie=cookie)
        assert status == HTTPStatus.CONFLICT
        assert len(seen) >= 2
        assert "Content-Disposition" not in headers
        assert not payload.startswith(b"PK\x03\x04")
        assert before == _files(tmp_path)
        assert not client.calls
