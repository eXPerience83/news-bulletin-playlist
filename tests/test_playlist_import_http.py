from __future__ import annotations

import http.client
import json
import re

import pytest
from _html import assert_valid_admin_html
from test_playlist_import import donor, rewrite
from test_source_admin_http import _csrf, _Handler, _login, _request, _server
from test_source_configuration_export import _files, _forbid_effects
from test_source_import_http import _multipart

from news_bulletin_playlist.managed_state import ManagedState, ManagedStateStore
from news_bulletin_playlist.playlist_import_web import imported_draft_editor
from news_bulletin_playlist.portable_playlists import canonical_json

ROOT = "/admin/playlists/import"


def post(server, cookie, document, token, headers=None):
    kind, body = _multipart(token, document)
    body = body.replace(b"Content-Type: application/json", b"Content-Type: application/zip")
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    extra = {"Content-Type": kind} | ({"Cookie": cookie} if cookie else {}) | (headers or {})
    connection.request("POST", ROOT + "/preview", body=body, headers=extra)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def form(page):
    match = re.search(rb'name="preview_id" value="([A-Za-z0-9_-]+)"', page)
    assert match
    return {
        "csrf_token": _csrf(page),
        "preview_id": match[1].decode(),
        "action": "apply",
        "confirm": "1",
        "ack_schema": "1",
    }


def preview(server, cookie, document):
    page = _request(server, "GET", ROOT, cookie=cookie)[2]
    status, _, page = post(server, cookie, document, _csrf(page))
    assert status == 200, page
    return page


@pytest.mark.parametrize(
    "path", [ROOT, ROOT + "/preview", ROOT + "/apply", "/admin/playlists/imported/update"]
)
def test_authentication(tmp_path, path):
    with _server(tmp_path) as (server, _, store):
        original = _files(tmp_path)
        assert _request(server, "GET" if path == ROOT else "POST", path)[0] in {303, 401, 403}
        assert _files(tmp_path) == original


def test_preview_is_readonly_and_apply_is_single_offline_commit(tmp_path, monkeypatch):
    document = donor(tmp_path)
    with _server(tmp_path) as (server, client, store):
        store.save(ManagedState())
        cookie = _login(server)
        _request(server, "GET", "/admin/sources/new", cookie=cookie)
        before = _files(tmp_path)
        original_save = ManagedStateStore.save
        _forbid_effects(monkeypatch, _Handler.source_admin_service)
        page = preview(server, cookie, document)
        assert_valid_admin_html(page.decode())
        assert b"schema v10" not in page or b"v9" in page
        assert _files(tmp_path) == before and not client.calls
        saved = []

        def save(self, state):
            saved.append(state)
            return original_save(self, state)

        monkeypatch.setattr(ManagedStateStore, "save", save)
        status, headers, page = _request(
            server, "POST", ROOT + "/apply", cookie=cookie, form=form(page)
        )
        assert status == 200, page
        assert headers["Cache-Control"] == "no-store"
        assert len(saved) == 1 and saved[0].schema_version == 10
        assert_valid_admin_html(page.decode())
        assert not client.calls
        after = _files(tmp_path)
        page = preview(server, cookie, document)
        assert b"identical" in page
        assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form(page))[0] == 200
        assert len(saved) == 1 and _files(tmp_path) == after


@pytest.mark.parametrize(
    "change",
    [
        {"csrf_token": "wrong"},
        {"confirm": "0"},
        {"ack_schema": "0"},
        {"preview_id": "wrong"},
        {"decision_99": "keep_local"},
        {"document": "tampered"},
        {"confirm": ["1", "1"]},
    ],
)
def test_manipulated_apply_no_writes(tmp_path, change):
    document = donor(tmp_path)
    with _server(tmp_path) as (server, _, store):
        store.save(ManagedState())
        cookie = _login(server)
        page = preview(server, cookie, document)
        before = _files(tmp_path)
        assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form(page) | change)[
            0
        ] in {400, 403, 409}
        assert _files(tmp_path) == before


def test_sessions_stale_replay_and_cancel(tmp_path):
    document = donor(tmp_path)
    with _server(tmp_path) as (server, _, store):
        store.save(ManagedState())
        a = _login(server)
        b = _login(server)
        page = preview(server, a, document)
        assert _request(server, "POST", ROOT + "/apply", cookie=b, form=form(page))[0] == 403
        other = preview(server, b, document)
        assert (
            _request(
                server,
                "POST",
                ROOT + "/apply",
                cookie=b,
                form=form(other) | {"preview_id": form(page)["preview_id"]},
            )[0]
            == 409
        )
        store.save(ManagedState(schema_version=7))
        before = _files(tmp_path)
        assert _request(server, "POST", ROOT + "/apply", cookie=a, form=form(page))[0] == 409
        assert _files(tmp_path) == before
        page = preview(server, a, document)
        values = form(page) | {"action": "cancel"}
        assert _request(server, "POST", ROOT + "/apply", cookie=a, form=values)[0] == 200
        assert _request(server, "POST", ROOT + "/apply", cookie=a, form=values)[0] == 403
        assert _files(tmp_path) == before


def test_unsupported_capabilities_shown_but_apply_blocked(tmp_path):
    def edit(m, d):
        data = json.loads(d["sources.json"])
        data["sources"][0]["processing"]["parser"]["id"] = "unknown"
        raw = canonical_json(data)
        d["sources.json"] = raw
        import hashlib

        m["files"]["sources.json"] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    document = rewrite(donor(tmp_path), edit)
    with _server(tmp_path) as (server, _, store):
        store.save(ManagedState())
        cookie = _login(server)
        before = _files(tmp_path)
        page = preview(server, cookie, document)
        assert b"unsupported" in page and b"Apply import" not in page
        assert_valid_admin_html(page.decode())
        assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form(page))[0] == 422
        assert _files(tmp_path) == before


def test_xss_and_oversized_upload(tmp_path):
    document = rewrite(
        donor(tmp_path),
        lambda m, d: m["playlist"].update(display_name='<img src=x onerror="alert(1)">'),
    )
    with _server(tmp_path) as (server, _, store):
        store.save(ManagedState())
        cookie = _login(server)
        page = preview(server, cookie, document)
        assert b"<img src=x" not in page and b"&lt;img" in page
        token = _csrf(_request(server, "GET", ROOT, cookie=cookie)[2])
        before = _files(tmp_path)
        assert (
            post(server, cookie, document, token, {"Content-Length": str(1024 * 1024 + 8193)})[0]
            == 413
        )
        assert _files(tmp_path) == before


def test_imported_editor_exact_seconds_exception_fields_and_no_oauth_activation(
    tmp_path, monkeypatch
):
    document = donor(tmp_path)
    with _server(tmp_path) as (server, _, store):
        store.save(ManagedState())
        cookie = _login(server)
        page = preview(server, cookie, document)
        assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form(page))[0] == 200
        draft = store.load_readonly().templates[0]
        csrf = _csrf(_request(server, "GET", ROOT, cookie=cookie)[2])
        editor = imported_draft_editor(draft, csrf)
        assert_valid_admin_html(
            '<!doctype html><html lang="en"><head><title>Edit</title>'
            '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            "<body><h1>Edit</h1>" + editor + "</body></html>"
        )
        fields = {
            name: value
            for name, value in re.findall(
                r'<input type="hidden" name="([^"]+)" value="([^"]*)">', editor
            )
        }
        fields |= {
            "display_name": "Edited",
            "description": "D" * 10000,
            "source_ids": "\n".join(draft.default_source_ids),
            "countries": ",".join(draft.countries),
            "languages": ",".join(draft.languages),
            "retention_hours": "9000",
            "max_episodes": "12000",
            "min_seconds": "121",
            "max_seconds": "100001",
            "exceptions": "[]",
            "cover_id": draft.cover_id,
        }
        status, _, body = _request(
            server, "POST", "/admin/playlists/imported/update", cookie=cookie, form=fields
        )
        assert status == 200, body
        updated = store.load_readonly().templates[0]
        assert (
            updated.min_duration_seconds == 121
            and updated.max_duration_seconds == 100001
            and updated.portable_id == draft.portable_id
        )
        assert updated.portable_metadata == draft.portable_metadata

        def forbidden(*a, **kw):
            pytest.fail("Activation fetched OAuth for imported draft")

        class Auth:
            get_access_token = forbidden

        monkeypatch.setattr(_Handler, "managed_admin_auth", Auth())
        csrf = _csrf(_request(server, "GET", ROOT, cookie=cookie)[2])
        assert _request(
            server,
            "POST",
            "/admin/playlists/activate",
            cookie=cookie,
            form={"csrf_token": csrf, "template_id": str(draft.id)},
        )[0] in {400, 409}


def test_preview_expiry_capacity_and_document_kind_binding(tmp_path):
    from datetime import UTC, datetime, timedelta

    from news_bulletin_playlist.admin_session import AdminSessionManager, SourceImportCapacityError
    from news_bulletin_playlist.catalog import BUILTIN_CATALOG
    from news_bulletin_playlist.portable_sources import export_sources

    manager = AdminSessionManager()
    now = datetime.now(UTC)
    document = donor(tmp_path)
    ids = []
    for _ in range(8):
        session = manager.create_session(now=now)
        ids.append(session)
        manager.stage_source_import(session, document, "fingerprint", now=now, kind="playlist")
    session = manager.create_session(now=now)
    with pytest.raises(SourceImportCapacityError):
        manager.stage_source_import(session, document, "fp", now=now, kind="playlist")
    context = manager.stage_source_import(ids[0], document, "fp", now=now, kind="playlist")
    assert manager.take_source_import(ids[0], context.preview_id, now=now) is None
    assert (
        manager.take_source_import(
            ids[0], context.preview_id, now=now + timedelta(minutes=11), kind="playlist"
        )
        is None
    )
    source = manager.stage_source_import(
        ids[0], export_sources((BUILTIN_CATALOG.sources[0],)), "fp", now=now
    )
    assert manager.take_source_import(ids[0], source.preview_id, now=now, kind="playlist") is None
    assert manager.take_source_import(ids[0], source.preview_id, now=now) == source


def test_expanded_form_reader_rejects_truncated_body():
    import io
    from types import SimpleNamespace

    from news_bulletin_playlist.runtime import HealthHandler

    request = SimpleNamespace(
        headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "20"},
        rfile=io.BytesIO(b"x=y"),
    )
    with pytest.raises(ValueError, match="Incomplete"):
        HealthHandler._read_form(request, max_bytes=100)


def test_import_and_edit_page_work_without_oauth_services(tmp_path, monkeypatch):
    document = donor(tmp_path)
    with _server(tmp_path) as (server, _, store):
        store.save(ManagedState())
        cookie = _login(server)
        _request(server, "GET", ROOT, cookie=cookie)
        source_service = _Handler.source_admin_service
        real_save = ManagedStateStore.save
        _forbid_effects(monkeypatch, source_service)
        monkeypatch.setattr(ManagedStateStore, "save", real_save)
        monkeypatch.setattr(_Handler, "managed_admin_service", None)
        monkeypatch.setattr(_Handler, "source_admin_service", None)
        page = preview(server, cookie, document)
        assert _request(server, "POST", ROOT + "/apply", cookie=cookie, form=form(page))[0] == 200
        draft = store.load_readonly().templates[0]
        before = _files(tmp_path)
        status, headers, page = _request(
            server, "GET", "/admin/playlists/imported/edit?id=" + str(draft.id), cookie=cookie
        )
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert_valid_admin_html(page.decode())
        assert _files(tmp_path) == before
