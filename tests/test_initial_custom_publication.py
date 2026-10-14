"""Initial custom Spotify metadata and cover are applied without duplicate creation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.managed_admin import (
    ManagedAdminError,
    ManagedAdminService,
    render_spotify_description,
)
from news_bulletin_playlist.managed_state import ManagedStateStore
from news_bulletin_playlist.spotify.client import SpotifyApiError, SpotifyTransportError

JPEG = b"\\xff\\xd8" + b"custom-default-cover" + b"\\xff\\xd9"


class _Spotify:
    def __init__(self, *, cover_error: Exception | None = None) -> None:
        self.created: list[tuple[str, str]] = []
        self.cover_calls: list[tuple[str, bytes]] = []
        self.metadata_calls: list[str] = []
        self.cover_error = cover_error
        self.service: ManagedAdminService | None = None
        self.observed_durable_before_cover = False

    def create_playlist(
        self, name: str, *, public: bool = True, description: str = ""
    ) -> dict[str, Any]:
        assert public is True
        self.created.append((name, description))
        return {"id": "A" * 22}

    def upload_playlist_cover(self, playlist_id: str, jpeg_bytes: bytes) -> dict[str, Any]:
        assert self.service is not None
        self.observed_durable_before_cover = (
            self.service.provisioning_journal.load() is None
            and any(
                p.destination.external_id == playlist_id
                for p in self.service.store.load().playlists
            )
        )
        self.cover_calls.append((playlist_id, jpeg_bytes))
        if self.cover_error is not None:
            raise self.cover_error
        return {}

    def change_playlist_details(
        self, playlist_id: str, *, name: str, description: str
    ) -> dict[str, Any]:
        self.metadata_calls.append(playlist_id)
        return {}


def _setup(tmp_path: Path, client: _Spotify, *, loader: Any = None) -> ManagedAdminService:
    service = ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: client,
        cover_loader=loader,
    )
    client.service = service
    return service


def _draft(service: ManagedAdminService, *, description: str = "Brief international news"):
    return service.create_local_template(
        display_name="My own playlist",
        description=description,
        cover_id="international_spanish_news",
        source_ids=("cnn",),
        retention_hours=48,
        max_episodes=100,
        min_duration_minutes=0,
        max_duration_minutes=30,
    )


def _publish(service: ManagedAdminService, draft: Any):
    return service.activate(
        template_id=draft.id,
        display_name=draft.display_name,
        description=draft.description,
        cover_id=draft.cover_id,
        source_ids=draft.default_source_ids,
        access_token="fake-access-token",
    )


def test_initial_custom_creation_applies_attribution_and_cover_after_durable_journal(
    tmp_path: Path,
) -> None:
    client = _Spotify()
    service = _setup(tmp_path, client, loader=lambda _cover_id: JPEG)
    draft = _draft(service)
    managed = _publish(service, draft)

    assert client.created == [(draft.display_name, render_spotify_description(draft.description))]
    assert client.cover_calls == [("A" * 22, JPEG)]
    assert client.observed_durable_before_cover
    assert client.metadata_calls == []  # description used the same creation request
    assert service.last_initial_cover_error is None
    assert managed.template_id == draft.id
    assert service.provisioning_journal.load() is None
    assert len(service.store.load().playlists) == 1


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (SpotifyApiError(403, "no cover permission"), "HTTP 403"),
        (SpotifyApiError(429, "rate limited"), "HTTP 429"),
        (SpotifyTransportError("connection dropped"), "network error"),
    ],
)
def test_initial_cover_failure_reports_pending_without_rollback_or_duplicate(
    tmp_path: Path, failure: Exception, expected: str
) -> None:
    client = _Spotify(cover_error=failure)
    service = _setup(tmp_path, client, loader=lambda _cover_id: JPEG)
    draft = _draft(service)

    managed = _publish(service, draft)
    assert managed.destination.external_id == "A" * 22
    assert service.last_initial_cover_error == expected
    assert client.created == [(draft.display_name, render_spotify_description(draft.description))]
    assert service.provisioning_journal.load() is None
    assert len(service.store.load().playlists) == 1
    assert len(client.cover_calls) == 1

    # A second activation must never duplicate the original Spotify destination.
    with pytest.raises(ManagedAdminError, match="already managed"):
        _publish(service, draft)
    assert len(client.created) == 1


def test_unavailable_local_cover_preserves_created_playlist(tmp_path: Path) -> None:
    client = _Spotify()

    def failing_loader(_cover: str) -> bytes:
        raise OSError("unsafe file path")

    service = _setup(tmp_path, client, loader=failing_loader)
    draft = _draft(service)
    _publish(service, draft)

    assert service.last_initial_cover_error == "local cover unavailable"
    assert len(client.created) == 1
    assert client.cover_calls == []


def test_long_custom_description_fails_before_journal_or_spotify_create(
    tmp_path: Path,
) -> None:
    client = _Spotify()
    service = _setup(tmp_path, client, loader=lambda _cover_id: JPEG)
    draft = _draft(service, description="a" * 200)
    with pytest.raises(ManagedAdminError, match="shorten the base description"):
        _publish(service, draft)
    assert client.created == []
    assert service.provisioning_journal.load() is None
    assert service.store.load().playlists == ()
