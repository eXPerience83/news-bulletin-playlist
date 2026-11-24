"""Bulk ZIP v3 export and read-only Admin integration."""

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
from news_bulletin_playlist.portable_playlists import PlaylistExportError

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
