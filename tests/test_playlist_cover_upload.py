from __future__ import annotations

import base64
import urllib.error
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.engine_runtime import _load_bundled_cover
from news_bulletin_playlist.managed_admin import ManagedAdminService, render_spotify_description
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.spotify.client import (
    SpotifyApiError,
    SpotifyClient,
    SpotifyTransportError,
)


class _Response:
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def read(self) -> bytes:
        return b""


class _CoverClient:
    def __init__(self, *, fail_cover: bool = False) -> None:
        self.fail_cover = fail_cover
        self.create_calls: list[tuple[str, str]] = []
        self.update_calls: list[tuple[str, str, str]] = []
        self.cover_calls: list[tuple[str, bytes]] = []

    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        self.create_calls.append((name, description))
        return {"id": "destination"}

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        self.update_calls.append((playlist_id, name, description))
        return {}

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        self.cover_calls.append((playlist_id, jpeg_bytes))
        if self.fail_cover:
            raise SpotifyApiError(403, "forbidden")
        return {}


class _CoverlessClient:
    def create_private_playlist(self, name: str, *, description: str = "") -> dict[str, Any]:
        del name, description
        return {"id": "destination"}

    def change_playlist_details(
        self,
        playlist_id: str,
        *,
        name: str,
        description: str,
    ) -> dict[str, Any]:
        del playlist_id, name, description
        return {}


class _Factory:
    def __init__(self, client: _CoverClient) -> None:
        self.client = client
        self.tokens: list[str] = []

    def __call__(self, token: str) -> _CoverClient:
        self.tokens.append(token)
        return self.client


def _activate(
    tmp_path: Path,
    client: _CoverClient,
    *,
    cover_loader: Any,
) -> tuple[ManagedAdminService, Any]:
    factory = _Factory(client)
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=factory,
        cover_loader=cover_loader,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )
    return service, managed


def test_spotify_cover_upload_sends_base64_jpeg_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    jpeg = b"\xff\xd8" + b"cover-bytes" + b"\xff\xd9"

    SpotifyClient("access-token").upload_playlist_cover("playlist-id", jpeg)

    request = captured["request"]
    assert isinstance(request, urllib.request.Request)
    assert request.full_url == "https://api.spotify.com/v1/playlists/playlist-id/images"
    assert request.method == "PUT"
    assert request.data == base64.b64encode(jpeg)
    assert request.get_header("Content-type") == "image/jpeg"
    assert request.get_header("Authorization") == "Bearer access-token"
    assert captured["timeout"] == 30.0


@pytest.mark.parametrize(
    ("failure_kind", "expected_exception"),
    [
        ("http", SpotifyApiError),
        ("transport", SpotifyTransportError),
    ],
)
def test_spotify_cover_upload_failure_does_not_expose_access_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_kind: str,
    expected_exception: type[RuntimeError],
) -> None:
    access_token = "access-token-sentinel"

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        assert request.get_header("Authorization") == f"Bearer {access_token}"
        assert timeout == 30.0
        if failure_kind == "http":
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                f"forbidden {access_token}",
                HTTPMessage(),
                None,
            )
        raise urllib.error.URLError(f"network {access_token}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    jpeg = b"\xff\xd8cover\xff\xd9"

    with pytest.raises(expected_exception) as exc_info:
        SpotifyClient(access_token).upload_playlist_cover("playlist-id", jpeg)

    captured = capsys.readouterr()
    assert access_token not in captured.out
    assert access_token not in captured.err
    assert access_token not in str(exc_info.value)


def test_spotify_cover_upload_rejects_invalid_or_oversized_payload_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_urlopen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("network must not be reached")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_urlopen)
    client = SpotifyClient("access-token")

    with pytest.raises(ValueError, match="JPEG"):
        client.upload_playlist_cover("playlist-id", b"not-a-jpeg")

    oversized = b"\xff\xd8" + (b"x" * 200_000) + b"\xff\xd9"
    with pytest.raises(ValueError, match="256 KiB"):
        client.upload_playlist_cover("playlist-id", oversized)


def test_bundled_spain_cover_is_a_spotify_sized_jpeg() -> None:
    jpeg = _load_bundled_cover("spain_spanish_news")

    assert jpeg.startswith(b"\xff\xd8")
    assert jpeg.endswith(b"\xff\xd9")
    assert len(base64.b64encode(jpeg)) <= 256 * 1024


def test_coverless_injected_client_skips_optional_cover_capability(tmp_path: Path) -> None:
    client = _CoverlessClient()
    loader_called = False

    def cover_loader(_cover_id: str) -> bytes:
        nonlocal loader_called
        loader_called = True
        return b"\xff\xd8cover\xff\xd9"

    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: client,  # type: ignore[arg-type]
        cover_loader=cover_loader,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")

    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    assert service.snapshot().managed == (managed,)
    assert not loader_called


def test_activation_uploads_cover_after_managed_state_is_persisted(tmp_path: Path) -> None:
    client = _CoverClient()
    jpeg = b"\xff\xd8cover\xff\xd9"

    service, managed = _activate(tmp_path, client, cover_loader=lambda _cover_id: jpeg)

    assert service.snapshot().managed == (managed,)
    assert client.cover_calls == [("destination", jpeg)]


def test_cover_api_failure_does_not_rollback_managed_playlist(tmp_path: Path) -> None:
    client = _CoverClient(fail_cover=True)
    jpeg = b"\xff\xd8cover\xff\xd9"

    service, managed = _activate(tmp_path, client, cover_loader=lambda _cover_id: jpeg)

    assert service.snapshot().managed == (managed,)
    assert client.cover_calls == [("destination", jpeg)]


def test_explicit_sync_reapplies_metadata_and_retries_cover(tmp_path: Path) -> None:
    client = _CoverClient(fail_cover=True)
    jpeg = b"\xff\xd8cover\xff\xd9"
    service, managed = _activate(tmp_path, client, cover_loader=lambda _cover_id: jpeg)
    client.fail_cover = False
    client.cover_calls.clear()
    client.update_calls.clear()

    synced = service.sync_spotify_metadata_and_cover(
        managed.id,
        access_token="reauthorized-token",
    )

    assert synced == managed
    assert client.update_calls == [
        (
            "destination",
            managed.display_name,
            render_spotify_description(managed.description),
        )
    ]
    assert client.cover_calls == [("destination", jpeg)]


def test_cover_failure_during_explicit_sync_does_not_fail_metadata_sync(tmp_path: Path) -> None:
    client = _CoverClient()
    jpeg = b"\xff\xd8cover\xff\xd9"
    service, managed = _activate(tmp_path, client, cover_loader=lambda _cover_id: jpeg)
    client.fail_cover = True
    client.cover_calls.clear()
    client.update_calls.clear()

    synced = service.sync_spotify_metadata_and_cover(managed.id, access_token="token")

    assert synced == managed
    assert len(client.update_calls) == 1
    assert client.cover_calls == [("destination", jpeg)]


def test_local_save_without_access_token_skips_cover_and_still_persists(tmp_path: Path) -> None:
    client = _CoverClient()
    service, managed = _activate(
        tmp_path,
        client,
        cover_loader=lambda _cover_id: b"\xff\xd8cover\xff\xd9",
    )
    client.cover_calls.clear()

    updated = service.update(
        managed.id,
        display_name=managed.display_name,
        description=managed.description,
        cover_id=managed.cover_id,
        source_ids=("ser",),
        enabled=True,
        access_token=None,
    )

    assert updated.source_ids == ("ser",)
    assert service.snapshot().managed == (updated,)
    assert client.cover_calls == []
