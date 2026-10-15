"""Owner image upload is local-only, CSRF-guarded and never mutates built-in destinations."""

from __future__ import annotations

import http.client
import io
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import pytest
from PIL import Image
from test_source_admin_http import _csrf, _Handler, _login, _request, _server

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.custom_cover_assets import (
    CustomCoverAssetStore,
    is_uploaded_cover_id,
)
from news_bulletin_playlist.managed_admin import ManagedAdminError, ManagedAdminService
from news_bulletin_playlist.managed_admin_web import render_managed_admin_page
from news_bulletin_playlist.managed_state import (
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.spotify.auth import AuthorizationState


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (640, 640), "#e6a346").save(output, format="PNG")
    return output.getvalue()


def _binary_form(csrf: str, kind: str, target_id: str, image: bytes) -> tuple[str, bytes]:
    boundary = b"owner-cover-test-boundary"
    parts: list[bytes] = []
    for name, value in (
        ("csrf_token", csrf.encode()),
        ("target_kind", kind.encode()),
        ("target_id", target_id.encode()),
    ):
        parts.append(
            b"--"
            + boundary
            + b"\r\n"
            + f'Content-Disposition: form-data; name="{name}"'.encode()
            + b"\r\n\r\n"
            + value
            + b"\r\n"
        )
    parts.extend(
        [
            b"--"
            + boundary
            + b"\r\n"
            + b'Content-Disposition: form-data; name="image"; filename="../../secret.png"\r\n'
            + b"Content-Type: image/png\r\n\r\n"
            + image
            + b"\r\n",
            b"--" + boundary + b"--\r\n",
        ]
    )
    return "multipart/form-data; boundary=owner-cover-test-boundary", b"".join(parts)


def _upload_http(server, cookie: str | None, content_type: str, body: bytes):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    headers = {"Content-Type": content_type}
    if cookie:
        headers["Cookie"] = cookie
    client.request("POST", "/admin/owner-covers/upload", body=body, headers=headers)
    response = client.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    client.close()
    return result


def _setup_service(store: ManagedStateStore, tmp_path: Path) -> ManagedAdminService:
    storage = CustomCoverAssetStore(tmp_path / "custom-covers")

    def no_spotify(_token: str):
        raise AssertionError("An image selection cannot call Spotify")

    return ManagedAdminService(
        store,
        client_factory=no_spotify,
        cover_loader=lambda cover: storage.load(cover),
    )


def _draft(service: ManagedAdminService):
    return service.create_local_template(
        display_name="My upload test",
        description="No changes to Spotify",
        cover_id="custom_default",
        source_ids=("cnn",),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=0,
        max_duration_minutes=30,
    )


def test_authenticated_upload_and_private_preview_do_not_call_spotify(tmp_path: Path) -> None:
    with _server(tmp_path) as (server, _unused_client, store):
        service = _setup_service(store, tmp_path)
        _Handler.managed_admin_service = service
        draft = _draft(service)
        cookie = _login(server)
        status, _, page = _request(server, "GET", "/admin/", cookie=cookie)
        assert status == HTTPStatus.OK
        assert 'enctype="multipart/form-data"' in page.decode()
        token = _csrf(page)
        kind, body = _binary_form(token, "draft", str(draft.id), _png())

        unauth_status, unauth_headers, _ = _upload_http(server, None, kind, body)
        assert unauth_status == HTTPStatus.SEE_OTHER
        assert unauth_headers["Location"] == "/admin/login"
        assert store.load().templates == (draft,)

        status, headers, response = _upload_http(server, cookie, kind, body)
        assert status == HTTPStatus.SEE_OTHER
        assert headers["Location"] == "/admin/?notice=owner-cover-uploaded"
        assert response == b""
        current = store.load().templates[0]
        assert is_uploaded_cover_id(current.cover_id)
        assert current.id == draft.id
        assert current.default_source_ids == draft.default_source_ids

        status, _, dashboard = _request(server, "GET", "/admin/", cookie=cookie)
        assert status == HTTPStatus.OK
        assert f"/admin/owner-covers/{current.cover_id}.jpg".encode() in dashboard
        assert 'name="cover_id"' in dashboard.decode()
        status, _, img = _request(
            server, "GET", f"/admin/owner-covers/{current.cover_id}.jpg", cookie=cookie
        )
        assert status == HTTPStatus.OK
        assert img.startswith(b"\xff\xd8")
        assert (
            _request(server, "GET", f"/admin/owner-covers/{current.cover_id}.jpg")[0]
            != HTTPStatus.OK
        )

        # A consumed CSRF token cannot overwrite the selected image.
        assert _upload_http(server, cookie, kind, body)[0] == HTTPStatus.FORBIDDEN
        assert store.load().templates[0].cover_id == current.cover_id


def test_managed_upload_changes_only_custom_local_selection(tmp_path: Path) -> None:
    storage = CustomCoverAssetStore(tmp_path / "custom-covers")
    store = ManagedStateStore(tmp_path / "managed-state.json")
    service = _setup_service(store, tmp_path)
    draft = _draft(service)
    existing = activate_template(
        BUILTIN_CATALOG.playlist("spain_spanish_news"),
        "first-production-destination",
    )
    custom_managed = activate_template(draft.as_playlist_template(), "custom-spotify-destination")
    snapshot = store.load()
    store.save(replace(snapshot, playlists=(existing, custom_managed)))

    image_id = storage.save(_png())
    service.select_uploaded_cover(
        target_kind="managed", target_id=str(custom_managed.id), cover_id=image_id
    )
    state = store.load()
    assert state.playlists[0] == existing
    assert state.templates == (draft,)
    assert state.playlists[1].cover_id == image_id
    assert state.playlists[1].destination == custom_managed.destination

    with pytest.raises(ManagedAdminError, match="Only custom"):
        service.select_uploaded_cover(
            target_kind="managed", target_id=str(existing.id), cover_id=image_id
        )
    assert store.load() == state

    with pytest.raises(ManagedAdminError, match="missing or invalid"):
        service.select_uploaded_cover(
            target_kind="draft",
            target_id=str(draft.id),
            cover_id="uploaded_" + "0" * 64,
        )


def test_managed_custom_can_return_to_bundled_cover_without_spotify(
    tmp_path: Path,
) -> None:
    store = ManagedStateStore(tmp_path / "managed-state.json")
    storage = CustomCoverAssetStore(tmp_path / "custom-covers")
    service = _setup_service(store, tmp_path)
    draft = _draft(service)
    original = activate_template(
        BUILTIN_CATALOG.playlist("spain_spanish_news"), "original-spotify-destination"
    )
    custom = activate_template(draft.as_playlist_template(), "new-custom-destination")
    store.save(replace(store.load(), playlists=(original, custom)))
    uploaded_id = storage.save(_png())
    service.select_uploaded_cover(
        target_kind="managed", target_id=str(custom.id), cover_id=uploaded_id
    )

    rendered = render_managed_admin_page(
        snapshot=service.snapshot(),
        catalog=BUILTIN_CATALOG,
        spotify_state=AuthorizationState.CONNECTED,
        csrf_token="csrf-value",
        last_cycle=None,
        lan_mode=False,
        state=store.load(),
    ).decode()
    sections = rendered.split('<article class="card playlist-card">')
    managed_custom = next(s for s in sections if "new-custom-destination" in s)
    built_in = next(s for s in sections if "original-spotify-destination" in s)
    assert '<select name="cover_id">' in managed_custom
    assert f'<option value="{uploaded_id}" selected>' in managed_custom
    assert '<option value="custom_default">' in managed_custom
    assert '<option value="international_spanish_news">' in managed_custom
    assert '<select name="cover_id">' not in built_in
    assert 'name="cover_id"' in built_in
    assert "uploads coming soon" not in rendered

    updated = service.update(
        custom.id,
        display_name=custom.display_name,
        description=custom.description,
        cover_id="custom_default",
        source_ids=custom.source_ids,
        enabled=custom.enabled,
        access_token=None,
    )
    assert updated.cover_id == "custom_default"
    state = store.load()
    assert state.playlists[0] == original
    assert state.playlists[1] == updated
    assert state.templates == (draft,)
    assert (storage.root / f"{uploaded_id}.jpg").exists()  # no unsafe asset deletion
