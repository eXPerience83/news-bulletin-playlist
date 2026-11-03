"""Initial playlist metadata and cover are single-shot, journal-safe fake-provider operations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from _cover_fixtures import _artwork

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import (
    MAX_SPOTIFY_BASE_DESCRIPTION_LENGTH,
    ManagedAdminError,
    ManagedAdminService,
    SpotifyPlaylistSyncResult,
    render_spotify_description,
)
from news_bulletin_playlist.managed_state import (
    ManagedState,
    ManagedStateStore,
    activate_template,
)
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError
from news_bulletin_playlist.spotify_backoff import SpotifyRateLimitSuppressed


class _Client:
    def __init__(self, cover_error: Exception | None = None) -> None:
        self.cover_error = cover_error
        self.create_calls: list[tuple[str, str]] = []
        self.cover_calls: list[tuple[str, bytes]] = []
        self.update_calls: list[tuple[str, str, str]] = []
        self.events: list[str] = []
        self.service: ManagedAdminService | None = None

    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, Any]:
        assert public and description.endswith("news-bulletin-playlist")
        self.events.append("create")
        self.create_calls.append((name, description))
        return {"id": "new-synthetic-destination"}

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        assert self.service is not None
        assert self.service.provisioning_journal.load() is None
        destination = self.service.store.load_readonly().playlists[-1].destination.external_id
        assert destination == playlist_id
        self.events.append("cover")
        self.cover_calls.append((playlist_id, jpeg_bytes))
        if self.cover_error is not None:
            raise self.cover_error
        return {}

    def change_playlist_details(
        self, playlist_id: str, *, name: str, description: str
    ) -> dict[str, Any]:
        self.events.append("metadata")
        self.update_calls.append((playlist_id, name, description))
        return {}


def _service(tmp_path: Path, client: _Client, *, cover_loader=None) -> ManagedAdminService:
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _: client,
        cover_loader=cover_loader if cover_loader is not None else lambda _: _artwork(),
    )
    client.service = service
    return service


def _activate_bundled(service: ManagedAdminService):
    template = BUILTIN_CATALOG.playlist("international_french_news")
    return service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=template.description,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="offline-test-token",
    )


def test_bundled_activation_uses_initial_attribution_then_cover_after_durable_identity(
    tmp_path: Path,
) -> None:
    client = _Client()
    service = _service(tmp_path, client)
    managed = _activate_bundled(service)
    source = BUILTIN_CATALOG.playlist("international_french_news")
    assert client.create_calls == [
        (source.display_name, render_spotify_description(source.description))
    ]
    assert client.events == ["create", "cover"]
    assert client.cover_calls == [("new-synthetic-destination", _artwork())]
    assert not client.update_calls
    assert service.last_initial_cover_error is None
    assert service.provisioning_journal.load() is None
    assert managed.destination.external_id == "new-synthetic-destination"


@pytest.mark.parametrize(
    "error",
    [
        SpotifyApiError(403, "forbidden"),
        SpotifyApiError(429, "cooldown"),
        SpotifyApiError(503, "server"),
        SpotifyTransportError("network timeout"),
        SpotifyRateLimitSuppressed("cooldown"),
    ],
)
def test_initial_cover_failure_never_reissues_create_and_explicit_retry_is_separate(
    tmp_path: Path, error: Exception
) -> None:
    client = _Client(cover_error=error)
    service = _service(tmp_path, client)
    managed = _activate_bundled(service)
    assert len(client.create_calls) == 1
    assert len(client.cover_calls) == 1
    assert service.last_initial_cover_error is not None
    assert len(service.store.load_readonly().playlists) == 1
    assert service.provisioning_journal.load() is None
    client.cover_error = None
    result = service.sync_spotify_metadata_and_cover(managed.id, access_token="offline-test-token")
    assert isinstance(result, SpotifyPlaylistSyncResult)
    assert len(client.create_calls) == 1
    assert len(client.cover_calls) == 2
    assert client.events == ["create", "cover", "metadata", "cover"]


def test_new_activation_leaves_four_existing_destinations_unmodified(tmp_path: Path) -> None:
    client = _Client()
    service = _service(tmp_path, client)
    template_ids = (
        "spain_spanish_news",
        "international_english_news",
        "international_polish_news",
        "international_spanish_long_news",
    )
    originals = tuple(
        replace(activate_template(BUILTIN_CATALOG.playlist(template_id), f"ORIGINAL-{i}"))
        for i, template_id in enumerate(template_ids)
    )
    service.store.save(ManagedState(schema_version=10, playlists=originals))
    managed = _activate_bundled(service)
    after = service.store.load_readonly()
    assert after.playlists[:4] == originals
    assert after.playlists[4] == managed
    assert client.cover_calls == [(managed.destination.external_id, _artwork())]
    assert client.update_calls == []
    assert len(client.create_calls) == 1


@pytest.mark.parametrize("overlong", [191, 200, 248])
def test_new_playlist_description_overflow_fails_before_journal_or_provider(
    tmp_path: Path, overlong: int
) -> None:
    assert overlong > MAX_SPOTIFY_BASE_DESCRIPTION_LENGTH
    client = _Client()
    service = _service(tmp_path, client)
    template = BUILTIN_CATALOG.playlist("international_french_news")
    with pytest.raises(ManagedAdminError, match="shorten.*190"):
        service.activate(
            template_id=template.id,
            display_name=template.display_name,
            description="x" * overlong,
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            access_token="offline-test-token",
        )
    assert not client.events
    assert service.provisioning_journal.load() is None
    assert service.store.load_readonly().playlists == ()
