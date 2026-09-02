from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import (
    PROJECT_DESCRIPTION_FOOTER,
    PROJECT_REPOSITORY_URL,
    ManagedAdminService,
    SpotifyPlaylistSyncError,
    render_spotify_bare_repository_description,
    render_spotify_description,
)
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyClient


class _Response:
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def read(self) -> bytes:
        return b""


class _DiagnosticClient:
    def __init__(
        self,
        *,
        rendered_status: int = 400,
        bare_url_status: int | None = None,
    ) -> None:
        self.rendered_status = rendered_status
        self.bare_url_status = bare_url_status
        self.name_calls: list[tuple[str, str]] = []
        self.description_calls: list[tuple[str, str]] = []
        self.cover_calls: list[tuple[str, bytes]] = []

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
        raise AssertionError("differential diagnostic client should not use combined metadata")

    def change_playlist_name(self, playlist_id: str, *, name: str) -> dict[str, Any]:
        self.name_calls.append((playlist_id, name))
        return {}

    def change_playlist_description(
        self,
        playlist_id: str,
        *,
        description: str,
    ) -> dict[str, Any]:
        self.description_calls.append((playlist_id, description))
        if PROJECT_DESCRIPTION_FOOTER in description:
            raise SpotifyApiError(
                self.rendered_status,
                "provider-body-sentinel access-token-sentinel",
            )
        if (
            self.bare_url_status is not None
            and description.endswith(f"\n{PROJECT_REPOSITORY_URL}")
        ):
            raise SpotifyApiError(
                self.bare_url_status,
                "bare-provider-sentinel refresh-token-sentinel",
            )
        return {}

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        self.cover_calls.append((playlist_id, jpeg_bytes))
        return {}


def _service_with_managed_playlist(
    tmp_path: Path,
    client: _DiagnosticClient,
) -> tuple[ManagedAdminService, Any, bytes]:
    jpeg = b"\xff\xd8cover\xff\xd9"
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: client,  # type: ignore[arg-type]
        cover_loader=lambda _cover_id: jpeg,
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="access-token-sentinel",
    )
    client.cover_calls.clear()
    return service, managed, jpeg


def test_explicit_sync_probes_bare_repository_url_after_rendered_http_400(
    tmp_path: Path,
) -> None:
    client = _DiagnosticClient(rendered_status=400)
    service, managed, jpeg = _service_with_managed_playlist(tmp_path, client)

    with pytest.raises(SpotifyPlaylistSyncError) as exc_info:
        service.sync_spotify_metadata_and_cover(
            managed.id,
            access_token="access-token-sentinel",
        )

    message = str(exc_info.value)
    assert "name applied" in message
    assert "description with project footer failed (HTTP 400)" in message
    assert "description with bare repository URL applied" in message
    assert "base description applied" not in message
    assert "cover applied" in message
    assert "access-token-sentinel" not in message
    assert "provider-body-sentinel" not in message
    assert client.name_calls == [("destination", managed.display_name)]
    assert client.description_calls == [
        ("destination", render_spotify_description(managed.description)),
        (
            "destination",
            render_spotify_bare_repository_description(managed.description),
        ),
    ]
    assert client.cover_calls == [("destination", jpeg)]


def test_explicit_sync_falls_back_to_base_if_bare_repository_url_also_gets_400(
    tmp_path: Path,
) -> None:
    client = _DiagnosticClient(rendered_status=400, bare_url_status=400)
    service, managed, jpeg = _service_with_managed_playlist(tmp_path, client)

    with pytest.raises(SpotifyPlaylistSyncError) as exc_info:
        service.sync_spotify_metadata_and_cover(
            managed.id,
            access_token="access-token-sentinel",
        )

    message = str(exc_info.value)
    assert "description with project footer failed (HTTP 400)" in message
    assert "description with bare repository URL failed (HTTP 400)" in message
    assert "base description applied" in message
    assert "cover applied" in message
    assert "provider-body-sentinel" not in message
    assert "bare-provider-sentinel" not in message
    assert "refresh-token-sentinel" not in message
    assert client.description_calls == [
        ("destination", render_spotify_description(managed.description)),
        (
            "destination",
            render_spotify_bare_repository_description(managed.description),
        ),
        ("destination", managed.description),
    ]
    assert client.cover_calls == [("destination", jpeg)]


def test_explicit_sync_does_not_probe_fallbacks_for_non_400(tmp_path: Path) -> None:
    client = _DiagnosticClient(rendered_status=403)
    service, managed, jpeg = _service_with_managed_playlist(tmp_path, client)

    with pytest.raises(SpotifyPlaylistSyncError) as exc_info:
        service.sync_spotify_metadata_and_cover(managed.id, access_token="token")

    message = str(exc_info.value)
    assert "description with project footer failed (HTTP 403)" in message
    assert "bare repository URL" not in message
    assert "base description" not in message
    assert client.description_calls == [
        ("destination", render_spotify_description(managed.description)),
    ]
    assert client.cover_calls == [("destination", jpeg)]


def test_spotify_client_diagnostic_metadata_requests_send_one_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        assert timeout == 30.0
        requests.append(request)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = SpotifyClient("access-token")

    client.change_playlist_name("playlist", name="Noticias España")
    client.change_playlist_description("playlist", description="Descripción")

    assert len(requests) == 2
    assert requests[0].method == "PUT"
    assert requests[1].method == "PUT"
    assert json.loads(requests[0].data or b"{}") == {"name": "Noticias España"}
    assert json.loads(requests[1].data or b"{}") == {"description": "Descripción"}
