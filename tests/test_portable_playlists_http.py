from __future__ import annotations

import re
from dataclasses import replace
from http import HTTPStatus

import pytest
from _html import assert_valid_admin_html
from test_custom_playlist_templates import _draft
from test_managed_sources import _state
from test_portable_playlists import UUID, _ready, _unpack
from test_source_admin_http import _Handler, _login, _request, _server
from test_source_configuration_export import _files, _forbid_effects

from news_bulletin_playlist.managed_provisioning import ProvisioningIntent, ProvisioningState
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.portable_playlists_web import portable_controls

EXPORT = "/admin/playlists/export.zip?kind=managed&id=spain_spanish_news"
PREPARE = "/admin/playlists/portable/prepare?kind=managed&id=spain_spanish_news"
POST = "/admin/playlists/portable/prepare"


def _form(page):
    return {
        name.decode(): value.decode()
        for name, value in re.findall(
            rb'<input type="hidden" name="([^"]+)" value="([^"]*)">', page
        )
    } | {"confirm": "yes", "ack_v9": "yes"}


@pytest.mark.parametrize("kind", ["managed", "draft"])
def test_download_and_identity_get_are_offline_readonly(tmp_path, monkeypatch, kind):
    with _server(tmp_path) as (server, client, store):
        state = replace(
            _ready(), templates=(_draft(portable_id="abb9730d-d6eb-4235-b9b4-1ea1c127d67c"),)
        )
        store.save(state)
        service = _Handler.source_admin_service
        p = state.playlists[0]
        service.provisioning_journal.save(
            ProvisioningIntent(
                state=ProvisioningState.DESTINATION_KNOWN,
                template_id=p.template_id,
                display_name="pending",
                description="private",
                cover_id=p.cover_id,
                source_ids=p.source_ids,
                retention_hours=p.retention_hours,
                max_episodes=p.max_episodes,
                max_duration_seconds=p.max_duration_seconds,
                destination_id="PRIVATE-DESTINATION",
            )
        )
        cookie = _login(server)
        _request(server, "GET", "/admin/sources/new", cookie=cookie)
        before = _files(tmp_path)
        _forbid_effects(monkeypatch, service)
        query = (
            "kind=managed&id=spain_spanish_news"
            if kind == "managed"
            else "kind=draft&id=custom_world_briefs"
        )
        status, headers, payload = _request(
            server, "GET", "/admin/playlists/export.zip?" + query, cookie=cookie
        )
        assert status == HTTPStatus.OK
        assert headers["Content-Type"] == "application/zip"
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Content-Disposition"].endswith('-v1.zip"')
        config, _ = _unpack(payload)
        assert config["kind"] == kind
        status, _, page = _request(
            server, "GET", "/admin/playlists/portable/prepare?" + query, cookie=cookie
        )
        assert status == 200
        assert_valid_admin_html(page.decode())
        assert before == _files(tmp_path)
        assert not client.calls


@pytest.mark.parametrize("path", [EXPORT, PREPARE, POST])
def test_authentication_required(tmp_path, path):
    with _server(tmp_path) as (server, _, store):
        store.save(_ready())
        original = store.path.read_bytes()
        assert _request(server, "POST" if path == POST else "GET", path)[0] in {303, 401, 403}
        assert store.path.read_bytes() == original


def test_preparation_explicit_csrf_ack_session_stale_and_idempotence(tmp_path):
    with _server(tmp_path) as (server, _, store):
        store.save(_state())
        cookie = _login(server)
        original = store.path.read_bytes()
        status, headers, _ = _request(server, "GET", EXPORT, cookie=cookie)
        assert status == 409 and "Content-Disposition" not in headers
        page = _request(server, "GET", PREPARE, cookie=cookie)[2]
        assert_valid_admin_html(page.decode())
        assert b"Older images cannot read" in page
        form = _form(page)
        assert store.path.read_bytes() == original
        assert (
            _request(server, "POST", POST, cookie=cookie, form=form | {"csrf_token": "wrong"})[0]
            == 403
        )
        no_ack = dict(form)
        no_ack.pop("ack_v9")
        assert _request(server, "POST", POST, cookie=cookie, form=no_ack)[0] == 409
        assert store.path.read_bytes() == original
        page = _request(server, "GET", PREPARE, cookie=cookie)[2]
        form = _form(page)
        another = _login(server)
        assert _request(server, "POST", POST, cookie=another, form=form)[0] == 403
        store.save(
            replace(_state(), playlists=(replace(_state().playlists[0], description="changed"),))
        )
        assert _request(server, "POST", POST, cookie=cookie, form=form)[0] == 409
        page = _request(server, "GET", PREPARE, cookie=cookie)[2]
        assert _request(server, "POST", POST, cookie=cookie, form=_form(page))[0] == 200
        loaded = store.load_readonly()
        assert loaded.schema_version == 9 and loaded.playlists[0].portable_id
        assert _request(server, "POST", POST, cookie=cookie, form=_form(page))[0] == 403
        current = store.path.read_bytes()
        assert _request(server, "GET", EXPORT, cookie=cookie)[0] == 200
        assert store.path.read_bytes() == current


@pytest.mark.parametrize(
    "suffix", ["&id=other", "&unknown=x", "#fragment", "&kind=draft", "&id=%3Cscript%3E", "x" * 513]
)
def test_bad_queries_do_not_write_or_emit_partial_zip(tmp_path, suffix):
    with _server(tmp_path) as (server, _, store):
        store.save(_ready())
        cookie = _login(server)
        original = store.path.read_bytes()
        status, headers, body = _request(server, "GET", EXPORT + suffix, cookie=cookie)
        assert status in {400, 409}
        assert "Content-Disposition" not in headers
        assert b"PK\x03\x04" not in body
        assert store.path.read_bytes() == original


def test_missing_cover_has_no_partial_download_or_writes(tmp_path, monkeypatch):
    with _server(tmp_path) as (server, _, store):
        store.save(_ready())
        cookie = _login(server)
        _request(server, "GET", "/admin/sources/new", cookie=cookie)
        before = _files(tmp_path)
        _forbid_effects(monkeypatch, _Handler.source_admin_service)

        def broken(*args, **kwargs):
            raise OSError("secret path /private")

        monkeypatch.setattr(
            "news_bulletin_playlist.portable_playlists.PortableCoverLoader.load", broken
        )
        status, headers, body = _request(server, "GET", EXPORT, cookie=cookie)
        assert status == 409 and "Content-Disposition" not in headers
        assert b"private" not in body
        assert before == _files(tmp_path)


def test_controls_escape_ids_and_confirmation_requires_no_side_effects():
    control = portable_controls("draft", 'evil"<script>', UUID)
    assert "<script>" not in control and "%3Cscript%3E" in control


def test_identity_post_only_one_state_save_no_other_effects(tmp_path, monkeypatch):
    with _server(tmp_path) as (server, _, store):
        store.save(_state())
        cookie = _login(server)
        _request(server, "GET", "/admin/sources/new", cookie=cookie)
        page = _request(server, "GET", PREPARE, cookie=cookie)[2]
        before = _files(tmp_path)
        original_save = ManagedStateStore.save
        saves = []
        _forbid_effects(monkeypatch, _Handler.source_admin_service)

        def save(self, state):
            saves.append(state)
            return original_save(self, state)

        monkeypatch.setattr(ManagedStateStore, "save", save)
        status, _, page = _request(server, "POST", POST, cookie=cookie, form=_form(page))
        assert status == 200 and len(saves) == 1
        after = _files(tmp_path)
        assert {k: v for k, v in before.items() if k != "managed-state.json"} == {
            k: v for k, v in after.items() if k != "managed-state.json"
        }
        assert set(before) == set(after)


@pytest.mark.parametrize(
    "mutation",
    [
        {"confirm": "no"},
        {"kind": "draft"},
        {"id": "other"},
        {"portable_id": UUID},
        {"fingerprint": "wrong"},
        {"ack_v9": "no"},
        {"confirm": ["yes", "yes"]},
    ],
)
def test_tampered_identity_confirmations_do_not_write(tmp_path, mutation):
    with _server(tmp_path) as (server, _, store):
        store.save(_state())
        cookie = _login(server)
        page = _request(server, "GET", PREPARE, cookie=cookie)[2]
        original = store.path.read_bytes()
        status, _, _ = _request(server, "POST", POST, cookie=cookie, form=_form(page) | mutation)
        assert status in {400, 409}
        assert store.path.read_bytes() == original
