from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from news_bulletin_playlist.catalog import BUILTIN_CATALOG
from news_bulletin_playlist.managed_admin import (
    LEGACY_PROJECT_DESCRIPTION_FOOTER,
    MAX_PLAYLIST_DESCRIPTION_LENGTH,
    PROJECT_DESCRIPTION_FOOTER,
    PROJECT_DESCRIPTION_SEPARATOR,
    PROJECT_REPOSITORY_URL,
    ManagedAdminError,
    ManagedAdminService,
    render_spotify_description,
)
from news_bulletin_playlist.managed_state import ManagedStateStore


class _Spotify:
    def __init__(self) -> None:
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
        return {}


def _service(tmp_path: Path, spotify: _Spotify) -> ManagedAdminService:
    return ManagedAdminService(
        ManagedStateStore(tmp_path / "managed-state.json"),
        client_factory=lambda _token: spotify,
        cover_loader=lambda _cover_id: b"\xff\xd8cover\xff\xd9",
    )


def test_renderer_appends_plain_text_repository_footer() -> None:
    rendered = render_spotify_description("Descripción")

    assert rendered == (f"Descripción{PROJECT_DESCRIPTION_SEPARATOR}{PROJECT_DESCRIPTION_FOOTER}")
    assert PROJECT_REPOSITORY_URL in rendered


@pytest.mark.parametrize(
    "contaminated",
    [
        f"Descripción\n\n{LEGACY_PROJECT_DESCRIPTION_FOOTER}",
        f"Descripción\r\n\r\n{LEGACY_PROJECT_DESCRIPTION_FOOTER}\r\n",
        f"Descripción\n\n{PROJECT_DESCRIPTION_FOOTER}",
        (f"Descripción\n\n{PROJECT_DESCRIPTION_FOOTER}\n\n{PROJECT_DESCRIPTION_FOOTER}"),
    ],
)
def test_renderer_strips_existing_terminal_footer_before_readding_one(contaminated: str) -> None:
    rendered = render_spotify_description(contaminated)

    assert rendered == (f"Descripción{PROJECT_DESCRIPTION_SEPARATOR}{PROJECT_DESCRIPTION_FOOTER}")
    assert rendered.count(PROJECT_REPOSITORY_URL) == 1


def test_managed_state_keeps_only_editable_base_description(tmp_path: Path) -> None:
    spotify = _Spotify()
    service = _service(tmp_path, spotify)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")

    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description="Base editable",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    assert managed.description == "Base editable"
    assert service.snapshot().managed[0].description == "Base editable"
    assert spotify.create_calls == [(template.display_name, "")]
    assert spotify.update_calls == []


def test_repeated_metadata_edits_sync_only_latest_value_with_one_project_footer(
    tmp_path: Path,
) -> None:
    spotify = _Spotify()
    service = _service(tmp_path, spotify)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description="Primera",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    first = service.update(
        managed.id,
        display_name="Noticias España 1",
        description="Segunda",
        cover_id=managed.cover_id,
        source_ids=managed.source_ids,
        enabled=True,
        access_token="unused-token",
    )
    service.update(
        managed.id,
        display_name="Noticias España 2",
        description=first.description,
        cover_id=managed.cover_id,
        source_ids=managed.source_ids,
        enabled=True,
        access_token="unused-token",
    )

    assert spotify.update_calls == []
    service.sync_spotify_metadata_and_cover(managed.id, access_token="sync-token")

    assert len(spotify.update_calls) == 1
    playlist_id, name, description = spotify.update_calls[0]
    assert playlist_id == "destination"
    assert name == "Noticias España 2"
    assert description == render_spotify_description("Segunda")
    assert description.count(PROJECT_REPOSITORY_URL) == 1


def test_metadata_update_strips_terminal_footer_from_editable_state(tmp_path: Path) -> None:
    spotify = _Spotify()
    service = _service(tmp_path, spotify)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description="Base editable",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    updated = service.update(
        managed.id,
        display_name=managed.display_name,
        description=f"Editada\r\n\r\n{PROJECT_DESCRIPTION_FOOTER}\r\n",
        cover_id=managed.cover_id,
        source_ids=managed.source_ids,
        enabled=True,
        access_token=None,
    )

    assert updated.description == "Editada"
    assert service.snapshot().managed[0].description == "Editada"
    assert spotify.update_calls == []

    service.sync_spotify_metadata_and_cover(managed.id, access_token="sync-token")
    assert spotify.update_calls[-1][2] == render_spotify_description("Editada")


def test_source_only_update_cleans_legacy_footer_without_spotify_metadata_write(
    tmp_path: Path,
) -> None:
    spotify = _Spotify()
    store = ManagedStateStore(tmp_path / "managed-state.json")
    service = ManagedAdminService(
        store,
        client_factory=lambda _token: spotify,
        cover_loader=lambda _cover_id: b"\xff\xd8cover\xff\xd9",
    )
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description="Base editable",
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )
    state = store.load()
    contaminated = replace(
        managed,
        description=f"Base editable\n\n{LEGACY_PROJECT_DESCRIPTION_FOOTER}",
    )
    store.save(replace(state, playlists=(contaminated,)))

    updated = service.update(
        managed.id,
        display_name=managed.display_name,
        description=contaminated.description,
        cover_id=managed.cover_id,
        source_ids=managed.source_ids[:-1],
        enabled=True,
        access_token=None,
    )

    assert updated.description == "Base editable"
    assert updated.source_ids == managed.source_ids[:-1]
    assert store.load().playlists[0].description == "Base editable"
    assert spotify.update_calls == []


def test_maximum_editable_description_fits_spotify_limit_with_footer(tmp_path: Path) -> None:
    spotify = _Spotify()
    service = _service(tmp_path, spotify)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    maximum = "x" * MAX_PLAYLIST_DESCRIPTION_LENGTH

    managed = service.activate(
        template_id=template.id,
        display_name=template.display_name,
        description=maximum,
        cover_id=template.cover_id,
        source_ids=template.default_source_ids,
        access_token="token",
    )

    assert managed.description == maximum
    assert spotify.create_calls[-1][1] == ""
    rendered = render_spotify_description(maximum)
    assert len(rendered) == 300
    assert rendered.endswith(PROJECT_DESCRIPTION_FOOTER)


def test_description_cannot_exceed_space_reserved_for_project_footer(tmp_path: Path) -> None:
    spotify = _Spotify()
    service = _service(tmp_path, spotify)
    template = BUILTIN_CATALOG.playlist("spain_spanish_news")
    too_long = "x" * (MAX_PLAYLIST_DESCRIPTION_LENGTH + 1)

    with pytest.raises(
        ManagedAdminError,
        match=f"at most {MAX_PLAYLIST_DESCRIPTION_LENGTH} characters",
    ):
        service.activate(
            template_id=template.id,
            display_name=template.display_name,
            description=too_long,
            cover_id=template.cover_id,
            source_ids=template.default_source_ids,
            access_token="token",
        )
